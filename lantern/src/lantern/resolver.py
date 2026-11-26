"""Deterministic per-room lighting resolver & 24h simulator."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .elastic import (
    apply_time_warp,
    compute_punch_residual,
    evaluate_brightness_spec,
    time_in_window,
)
from .models import (
    BrightnessBreakdown,
    ColorOutput,
    ConditionSpec,
    ParsedRule,
    PunchState,
    RoleEvaluation,
    RoomConfig,
    RoomEvaluation,
    RoomInputs,
    StateConfidence,
)
from .schemas import evaluate_schema, kelvin_to_rgb, lerp_rgb
from .standard import (
    HouseModel,
    LightSettings,
    curve_level,
    darkness_from_lux,
    darkness_from_sun,
    in_window,
)


def standard_darkness(house: HouseModel, room_cfg: RoomConfig, inputs: RoomInputs) -> tuple[float, str]:
    """Stage 2: raw darkness 0..1 and where it came from.

    Ambient daylight = room lux − sensor floor − what our own lights add (each light's lux_gain, learned or set;
    the old static bulb_lux_gain otherwise). With the sensor unavailable, Flint predicts daylight from the sun
    using the room's learned daylight model; with no model either, the sun's height alone decides.
    """
    curve = house.curve
    rm = house.rooms.get(room_cfg.room)
    if curve.darkness_source == "ambient" and inputs.lux_source in ("sensor", "sandbox"):
        own = 0.0
        for role, ents in room_cfg.bulbs.items():
            pct = 0.0
            for e in ents:
                st = inputs.entity_states.get(e, {})
                if st.get("state") == "on":
                    b = (st.get("attributes") or {}).get("brightness")
                    pct = max(pct, float(b) / 2.55 if b is not None else 100.0)
            ls = rm.lights.get(role) if rm else None
            gain = ls.lux_gain if ls and ls.lux_gain is not None else room_cfg.bulb_lux_gain.get(role, 40.0)
            own += gain * pct / 100.0
        floor = (rm.lux_base or 0.0) if rm else 0.0
        ambient = max(0.0, inputs.ambient_lux - floor - own)
        return darkness_from_lux(ambient, curve), (
            f"ambient {ambient:.0f} lx (room {inputs.ambient_lux:.0f} − own lights {own:.0f}"
            + (f" − sensor floor {floor:.1f}" if floor else "") + ")")
    if curve.darkness_source == "ambient" and rm and rm.daylight and inputs.sun_elev is not None:
        from .lux import predict_daylight

        day = predict_daylight(rm.daylight.model_dump(), inputs.sun_elev) or 0.0
        return darkness_from_lux(day, curve), f"predicted daylight {day:.0f} lx from the sun at {inputs.sun_elev:.0f}° (lux sensor unavailable)"
    d = darkness_from_sun(inputs.sun_elev)
    if d is not None:
        return d, f"sun {inputs.sun_elev:.0f}°"
    return darkness_from_lux(max(0.0, inputs.ambient_lux), curve), f"room lux {inputs.ambient_lux:.0f} (no sun data)"


def _condition_matches(
    cond: ConditionSpec,
    effective_time: datetime,
    inputs: RoomInputs,
) -> bool:
    if cond.kind == "any":
        return True
    if cond.kind == "zone":
        return bool(inputs.zones.get(cond.zone_name or "", False))
    if cond.kind == "state":
        return (inputs.learned_state or "").lower() == (cond.state_name or "").lower()
    if cond.kind == "time" and cond.start_minutes is not None and cond.end_minutes is not None:
        m = effective_time.hour * 60 + effective_time.minute + effective_time.second / 60.0
        matched, _ = time_in_window(m, cond.start_minutes, cond.end_minutes)
        return matched
    return False


_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_GROUPS = {"weekday": _DAYS[:5], "weekend": _DAYS[5:]}
_ENTITY_DOMAINS = ("switch.", "input_boolean.", "binary_sensor.", "sensor.", "media_player.", "light.",
                   "input_select.", "person.", "device_tracker.", "sun.")


def _day_set(rhs: str) -> set[str]:
    out: set[str] = set()
    for part in rhs.replace(" ", "").split(","):
        out.update(_DAY_GROUPS.get(part, [part[:3]]))
    return out


def _hhmm(v: str) -> int:
    h, m = v.strip().split(":", 1)
    return int(h) * 60 + int(m)


def _atom(atom: str, inputs: RoomInputs, now: datetime | None) -> bool:
    expr = atom.strip().lower()
    if expr in ("true", "always"):
        return True
    if expr.startswith("not "):
        return not _atom(expr[4:], inputs, now)

    # day == fri | day in fri,sat | day == weekend
    if expr.startswith("day "):
        if now is None:
            return False
        today = _DAYS[now.weekday()]
        op, _, rhs = expr[4:].strip().partition(" ")
        if op in ("==", "in"):
            return today in _day_set(rhs)
        if op == "!=":
            return today not in _day_set(rhs)
        return False

    # time >= 17:00 | time < 23:00 | time in 17:00-02:00 (wraps midnight)
    if expr.startswith("time "):
        if now is None:
            return False
        m = now.hour * 60 + now.minute
        op, _, rhs = expr[5:].strip().partition(" ")
        if op == "in" and "-" in rhs:
            a, b = (_hhmm(x) for x in rhs.split("-", 1))
            return a <= m < b if a <= b else (m >= a or m < b)
        t = _hhmm(rhs)
        return {">=": m >= t, ">": m > t, "<=": m <= t, "<": m < t, "==": m == t}.get(op, False)

    for op in ("!=", "=="):
        if op in expr:
            lhs, rhs = [x.strip() for x in expr.split(op, 1)]
            res = _equals(lhs, rhs, inputs)
            return (not res) if op == "!=" else res
    return False


def _equals(lhs: str, rhs: str, inputs: RoomInputs) -> bool:
    if lhs == "media":
        return inputs.media_state.lower() == rhs or (rhs == "playing" and inputs.media_state.lower() == "buffering")
    if lhs == "bedtime":
        return ("on" if inputs.bedtime else "off") == rhs
    if lhs == "state":
        return (inputs.learned_state or "").lower() == rhs
    if lhs == "occupied":
        return ("on" if inputs.occupied else "off") == rhs
    if lhs.startswith("zone."):
        z = lhs.split(".", 1)[1]
        return ("on" if inputs.zones.get(z, False) else "off") == rhs
    # Any HA entity: switch.luxie_party_lounge == on
    if lhs.startswith(_ENTITY_DOMAINS):
        return str(inputs.entity_states.get(lhs, {}).get("state", "")).lower() == rhs
    return False


def evaluate_overlay_condition(when_expr: str, inputs: RoomInputs, now: datetime | None = None) -> bool:
    """Evaluate a safe overlay expression (no eval). `or` binds looser than `and`.

    Atoms: media == playing | bedtime == on | occupied == on | zone.sofa == on | state == working
           day == fri | day in fri,sat | day == weekend | time >= 17:00 | time in 17:00-02:00
           switch.luxie_party_lounge == on (any HA entity) | not <atom>
    e.g.  "switch.luxie_party_lounge == on or (day == fri and time >= 17:00)" -> write without brackets:
          "switch.luxie_party_lounge == on or day == fri and time >= 17:00"
    """
    expr = f" {when_expr.strip().lower()} "
    return any(
        all(_atom(a, inputs, now) for a in clause.split(" and "))
        for clause in expr.split(" or ")
    )


def compute_state_confidence(inputs: RoomInputs, now: datetime) -> StateConfidence:
    """Name what the room is doing from its discovered state; no hardcoded guesses."""
    if inputs.state_name or inputs.learned_state:
        name = inputs.state_name or (inputs.learned_state or "").replace("_", " ").title()
        return StateConfidence(inferred_state=name, state_source="state")
    return StateConfidence(inferred_state="Occupied" if inputs.occupied else "Empty", state_source="presence")


def blend_color(a: ColorOutput, b: ColorOutput, w: float) -> ColorOutput:
    """Ease from colour a toward b by weight w in [0, 1] (so a TV overlay drifts in instead of snapping)."""
    w = max(0.0, min(1.0, w))
    if w >= 0.999 or a.mode == "keep":
        return b
    if w <= 0.001 or b.mode == "keep":
        return a
    if a.mode == "kelvin" and b.mode == "kelvin" and a.kelvin and b.kelvin:
        k = int(round(a.kelvin + (b.kelvin - a.kelvin) * w))
        return b.model_copy(update={"kelvin": k, "rgb": kelvin_to_rgb(k), "stage": f"blending {int(w*100)}%"})
    rgb_a = a.rgb or kelvin_to_rgb(a.kelvin or 3000)
    rgb_b = b.rgb or kelvin_to_rgb(b.kelvin or 3000)
    return b.model_copy(update={"mode": "rgb", "rgb": lerp_rgb(rgb_a, rgb_b, w), "kelvin": None,
                                "stage": f"blending {int(w*100)}%"})


def _bucket_for_time(effective_time: datetime, active_overlays: list[str]) -> str:
    if "bedtime" in active_overlays:
        return "bedtime"
    if "tv_watching" in active_overlays:
        return "tv_watching"
    h = effective_time.hour
    if 6 <= h < 18:
        return "day"
    if 18 <= h < 21:
        return "evening"
    return "night"


def resolve_room(
    room_cfg: RoomConfig,
    schemas: dict[str, dict[str, Any]],
    inputs: RoomInputs,
    now: datetime,
    punches: dict[str, PunchState] | None = None,
    mode_entered_at: datetime | None = None,
    overlay_weights: dict[str, float] | None = None,
    seasonal: dict[str, Any] | None = None,
    house: HouseModel | None = None,
    pins: dict[str, dict[str, Any]] | None = None,
) -> RoomEvaluation:
    """Resolve desired target & causal breakdown for every role in a room.

    overlay_weights: name -> 0..1 blend weight maintained by the runtime (eases overlays in/out over
    `blend_minutes`). If omitted, overlays whose condition is true apply at full weight.
    """
    punches = punches or {}

    weights: dict[str, float] = {}
    for ov in room_cfg.overlays:
        if overlay_weights is not None:
            weights[ov.name] = max(0.0, min(1.0, overlay_weights.get(ov.name, 0.0)))
        else:
            weights[ov.name] = 1.0 if evaluate_overlay_condition(ov.when, inputs, now) else 0.0
    active_overlays = [
        ov for ov in sorted(room_cfg.overlays, key=lambda o: o.priority) if weights[ov.name] > 0.0
    ]
    warp_minutes = int(round(sum(ov.time_warp_minutes * weights[ov.name] for ov in active_overlays)))
    season = seasonal if (seasonal and seasonal.get("applies")) else None
    if season:
        warp_minutes += int(season.get("warp_minutes", 0))
    effective_time = apply_time_warp(now, warp_minutes)
    active_overlay_names = [ov.name for ov in active_overlays]
    bucket_key = _bucket_for_time(effective_time, [ov.name for ov in active_overlays if weights[ov.name] >= 0.5])
    elapsed_in_mode = (
        max(0.0, (now - mode_entered_at).total_seconds()) if mode_entered_at else 15.0
    )

    roles_out: dict[str, RoleEvaluation] = {}
    confidence = compute_state_confidence(inputs, now)
    pins = pins or {}

    room_model = house.rooms.get(room_cfg.room) if house else None
    standard = room_model is not None and room_model.engine == "standard"
    if standard:
        raw_d, d_src = standard_darkness(house, room_cfg, inputs)
        darkness = inputs.darkness if inputs.darkness is not None else raw_d
        minute = effective_time.hour * 60 + effective_time.minute
        night = in_window(minute, house.curve.night_from, house.curve.night_to)

    for role, entities in room_cfg.bulbs.items():
        offline = [
            e for e in entities
            if inputs.entity_states.get(e, {}).get("state") in ("unavailable", "unknown")
        ]
        online = [e for e in entities if e not in offline]

        # Current brightness estimate from HA state cache (for lux-gain anti-oscillation)
        current_pct = 0.0
        for e in online:
            attrs = inputs.entity_states.get(e, {}).get("attributes", {})
            if "brightness" in attrs and attrs["brightness"] is not None:
                current_pct = max(current_pct, float(attrs["brightness"]) / 2.55)

        trace: list[dict[str, Any]] = []
        zname, zone = (house.owner(room_cfg.room, role) if standard else (None, None))
        ls = (room_model.lights.get(role) or LightSettings()) if standard else None
        pin = pins.get(role)
        if standard and (not ls.enabled or (pin and pin.get("mode") == "ignore")):
            why = "disabled in its settings" if not ls.enabled else "pinned 'ignore' in the switchboard"
            roles_out[role] = RoleEvaluation(
                role=role, entities=entities, on=False, brightness_pct=0, hands_off=True, zone=zname,
                color=ColorOutput(mode="keep", stage="hands_off"),
                breakdown=BrightnessBreakdown(rule_matched=f"hands off ({why})", bucket_key=bucket_key,
                                              trace=[{"stage": "pins", "out": None, "note": f"hands off: {why}"}]),
                offline_entities=offline,
            )
            continue

        if not inputs.occupied and not (standard and pin and pin.get("mode") == "pinned"):
            roles_out[role] = RoleEvaluation(
                role=role,
                entities=entities,
                on=False,
                brightness_pct=0,
                color=ColorOutput(mode="keep", stage="unoccupied"),
                breakdown=BrightnessBreakdown(
                    final_pct=0,
                    rule_matched="unoccupied -> off",
                    bucket_key=bucket_key,
                    notes=["Room clear; lights off"] + ([f"Skipped offline: {offline}"] if offline else []),
                    trace=[{"stage": "presence", "out": 0, "note": "room empty → off"}] if standard else [],
                ),
                offline_entities=offline,
                zone=zname,
            )
            continue

        # 1. Resolve Base Layer: the general curve (Standard) or a hand-written rule (legacy)
        base_rule: ParsedRule | None = None
        if not standard:
            for rule in room_cfg.rules:
                if rule.role == role and _condition_matches(rule.condition, effective_time, inputs):
                    base_rule = rule
                    break

        gain = room_cfg.bulb_lux_gain.get(role, 40.0)
        if standard:
            curve = house.curve
            zone_here = zone.mode == "join" or bool(inputs.zones.get(zname, False))
            trace.append({"stage": "presence", "out": None,
                          "note": f"room occupied; zone {zname} ({zone.mode})" + ("" if zone_here else f" empty → idle {zone.idle_pct:.0f}%")})
            trace.append({"stage": "darkness", "out": round(darkness, 2), "note": d_src})
            lvl = curve_level(darkness, curve)
            note = f"curve({darkness:.2f}) = {lvl:.0f}%"
            if night and lvl > curve.night_max_pct:
                lvl = curve.night_max_pct
                note += f", night cap {curve.night_max_pct:.0f}%"
            trace.append({"stage": "curve", "out": round(lvl, 1), "note": note})
            if not zone_here:
                lvl = zone.idle_pct if lvl > 0 else 0.0
                trace.append({"stage": "light", "out": round(lvl, 1), "note": "zone idle level"})
            elif lvl > 0:
                if ls.curve == "fixed":
                    lvl, how = ls.fixed_pct, f"fixed {ls.fixed_pct:.0f}%"
                elif ls.curve == "switch":
                    lvl, how = 100.0, "switch: on"
                else:
                    lvl = lvl * ls.scale + ls.offset_pct
                    how = f"× {ls.scale:g} {ls.offset_pct:+.0f}%"
                trace.append({"stage": "light", "out": round(max(0.0, lvl), 1), "note": f"{ls.curve}: {how}"})
            base_pct = max(0.0, min(100.0, lvl))
            if ls.curve == "natural":
                base_color = evaluate_schema(curve.colour_schema, schemas, effective_time, elapsed_in_mode)
                if ls.kelvin_offset and base_color.mode == "kelvin" and base_color.kelvin:
                    k = base_color.kelvin + ls.kelvin_offset
                    base_color = base_color.model_copy(update={"kelvin": k, "rgb": kelvin_to_rgb(k)})
            else:
                base_color = ColorOutput(mode="keep", stage="light leaves colour alone")
            matched_desc = f"standard curve · zone {zname}"
        elif base_rule:
            base_pct = evaluate_brightness_spec(
                base_rule.brightness,
                base_rule.condition,
                effective_time,
                inputs.ambient_lux,
                gain,
                current_pct,
            )
            base_color = evaluate_schema(
                base_rule.schema_name, schemas, effective_time, elapsed_in_mode
            )
            matched_desc = base_rule.raw
        else:
            base_pct = 0.0
            base_color = ColorOutput(mode="keep", stage="no_base_rule")
            matched_desc = "no base rule matched"

        # 2. Apply matching Overlays in priority order
        overlay_delta = 0.0
        working_pct = base_pct
        working_color = base_color

        for ov in active_overlays:
            for ov_rule in ov.rules:
                if ov_rule.role == role and _condition_matches(ov_rule.condition, effective_time, inputs):
                    new_pct = evaluate_brightness_spec(
                        ov_rule.brightness,
                        ov_rule.condition,
                        effective_time,
                        inputs.ambient_lux,
                        gain,
                        current_pct,
                        base_pct_for_dim=working_pct,
                    )
                    w = weights[ov.name]
                    blended_pct = working_pct + (new_pct - working_pct) * w
                    overlay_delta += (blended_pct - working_pct)
                    working_pct = blended_pct
                    ov_color = evaluate_schema(
                        ov_rule.schema_name,
                        schemas,
                        effective_time,
                        elapsed_in_mode,
                        prior_color=working_color,
                    )
                    working_color = blend_color(working_color, ov_color, w)
                    matched_desc = f"[{ov.name}{'' if w >= 0.999 else f' {int(w*100)}%'}] {ov_rule.raw}"
                    break

        # 3. Apply Luxie Learned Baseline Offset + User Punching-Bag Residual
        # Only add learned/punch offsets if role is active (>0) or explicitly punched on
        punch_residual = compute_punch_residual(punches.get(role), now)

        notes: list[str] = []
        if season and season.get("warp_minutes"):
            notes.append(f"Season: evening {'later' if season['warp_minutes'] < 0 else 'earlier'} by {abs(season['warp_minutes'])} min")
        for ov in active_overlays:
            if weights[ov.name] < 0.999:
                notes.append(f"{ov.name} easing ({int(weights[ov.name]*100)}%)")
        if warp_minutes:
            notes.append(f"TV/overlay warped clock +{warp_minutes}m -> {effective_time.strftime('%H:%M')}")
        if base_rule and base_rule.brightness.kind == "target_lux":
            notes.append(f"Target {base_rule.brightness.value:.0f} lux vs ambient {inputs.ambient_lux:.0f} lux (gain {gain:.0f})")
        if offline:
            notes.append(f"Skipped offline bulbs cleanly: {', '.join(offline)}")

        if working_pct <= 0.0 and punch_residual <= 0.0:
            final_pct = 0
            applied_learned = 0.0
        else:
            applied_learned = 0.0
            final_pct = int(max(0, min(100, round(working_pct + punch_residual))))
            if season and final_pct > 0 and season.get("boost_now_pct"):
                boosted = int(min(100, round(final_pct * (1 + season["boost_now_pct"] / 100.0))))
                if boosted != final_pct:
                    notes.append(f"Winter lift +{season['boost_now_pct']:.0f}% ({season['day']['day_hours']:.1f} h of daylight)")
                final_pct = boosted
            if season and final_pct > 0 and season.get("kelvin_now") and working_color.mode == "kelvin" and working_color.kelvin:
                k = int(working_color.kelvin + season["kelvin_now"])
                working_color = working_color.model_copy(update={"kelvin": k, "rgb": kelvin_to_rgb(k)})

        if standard:
            if overlay_delta or season:
                trace.append({"stage": "layers", "out": round(working_pct, 1),
                              "note": ", ".join(n for n in notes if "easing" in n or "Winter" in n or "Season" in n) or "overlays"})
            if punch_residual:
                trace.append({"stage": "manual", "out": final_pct, "note": f"manual nudge {punch_residual:+.0f}% (decaying)"})
            if final_pct > 0 and ls.curve != "switch":
                hi = ls.max_pct
                if night:
                    hi = min(hi, house.curve.night_max_pct)
                clamped = int(max(ls.min_pct, min(hi, final_pct)))
                if clamped != final_pct:
                    trace.append({"stage": "clamp", "out": clamped, "note": f"light range {ls.min_pct:.0f}–{hi:.0f}%"})
                final_pct = clamped
            if pin and pin.get("mode") in ("pinned", "off"):
                final_pct = int(pin.get("brightness") or 0) if pin["mode"] == "pinned" else 0
                if pin["mode"] == "pinned" and pin.get("kelvin"):
                    k = int(pin["kelvin"])
                    working_color = ColorOutput(mode="kelvin", kelvin=k, rgb=kelvin_to_rgb(k), stage="pinned")
                trace.append({"stage": "pins", "out": final_pct, "note": f"switchboard: {pin['mode']}"})
            trace.append({"stage": "output", "out": final_pct, "note": "on" if final_pct else "off"})

        roles_out[role] = RoleEvaluation(
            role=role,
            entities=entities,
            on=final_pct > 0,
            brightness_pct=final_pct,
            zone=zname,
            color=working_color,
            breakdown=BrightnessBreakdown(
                base_pct=round(base_pct, 1),
                time_warp_minutes=warp_minutes,
                overlay_delta_pct=round(overlay_delta, 1),
                learned_baseline_pct=round(applied_learned, 1),
                active_punch_pct=round(punch_residual, 1),
                final_pct=final_pct,
                rule_matched=matched_desc,
                bucket_key=bucket_key,
                notes=notes,
                trace=trace,
            ),
            offline_entities=offline,
        )

    return RoomEvaluation(
        room=room_cfg.room,
        timestamp=now,
        effective_time=effective_time,
        active_overlays=active_overlay_names,
        roles=roles_out,
        confidence=confidence,
    )
