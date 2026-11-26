"""Deterministic per-room lighting resolver & 24h simulator."""

from __future__ import annotations

from datetime import datetime, timedelta
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
    """Estimate room intent confidence (for Luxie active-learning confirmations)."""
    hour = now.hour + now.minute / 60.0
    is_late = hour >= 22.0 or hour < 5.5
    desk_active = any(v for k, v in inputs.zones.items() if "desk" in k.lower())

    if inputs.media_state.lower() in ("playing", "buffering"):
        tv_conf = 0.96
    elif inputs.zones.get("sofa", False) and not desk_active:
        tv_conf = 0.55
    else:
        tv_conf = 0.1

    work_conf = 0.92 if desk_active else (0.65 if 8.5 <= hour <= 17.5 and inputs.occupied else 0.1)

    sleep_score = 0.0
    if is_late:
        sleep_score += 0.35
    if inputs.media_state.lower() not in ("playing", "buffering"):
        sleep_score += 0.20
    if inputs.idle_minutes >= 15:
        sleep_score += 0.25
    elif inputs.idle_minutes >= 8:
        sleep_score += 0.12
    if inputs.ambient_lux < 12:
        sleep_score += 0.15
    if desk_active:
        sleep_score -= 0.45
    if not inputs.occupied:
        sleep_score += 0.10

    sleep_conf = max(0.0, min(1.0, sleep_score))

    source = "heuristic"
    if inputs.bedtime or sleep_conf >= 0.90:
        inferred = "Sleeping"
    elif inputs.learned_state:
        inferred = inputs.learned_state.replace("_", " ").title()
        source = "learned" if inputs.learned_match == "exact" else "learned~"
    elif tv_conf >= 0.85:
        inferred = "Watching TV"
    elif work_conf >= 0.80:
        inferred = "Working"
    elif inputs.occupied:
        inferred = "Relaxing"
    else:
        inferred = "Empty"

    needs_confirm = (0.60 <= sleep_conf < 0.90) and not inputs.bedtime and inputs.occupied
    prompt = (
        f"Room quiet for {int(inputs.idle_minutes)}m & TV idle ({int(sleep_conf*100)}% sure) — switch to Sleep Mode?"
        if needs_confirm
        else None
    )

    return StateConfidence(
        inferred_state=inferred,
        state_source=source,
        sleep_confidence=round(sleep_conf, 2),
        working_confidence=round(work_conf, 2),
        tv_confidence=round(tv_conf, 2),
        needs_confirmation=needs_confirm,
        prompt_text=prompt,
    )


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
    learned_offsets: dict[str, float] | None = None,
    mode_entered_at: datetime | None = None,
    overlay_weights: dict[str, float] | None = None,
    seasonal: dict[str, Any] | None = None,
) -> RoomEvaluation:
    """Resolve desired target & causal breakdown for every role in a room.

    overlay_weights: name -> 0..1 blend weight maintained by the runtime (eases overlays in/out over
    `blend_minutes`). If omitted, overlays whose condition is true apply at full weight.
    """
    punches = punches or {}
    learned_offsets = learned_offsets or {}

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

        if not inputs.occupied:
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
                ),
                offline_entities=offline,
            )
            continue

        # 1. Resolve Base Layer rule
        base_rule: ParsedRule | None = None
        for rule in room_cfg.rules:
            if rule.role == role and _condition_matches(rule.condition, effective_time, inputs):
                base_rule = rule
                break

        gain = room_cfg.bulb_lux_gain.get(role, 40.0)
        if base_rule:
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
        learned_offset = learned_offsets.get(f"{role}:{bucket_key}", 0.0)
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
            applied_learned = learned_offset if working_pct > 0 else 0.0
            final_pct = int(max(0, min(100, round(working_pct + applied_learned + punch_residual))))
            if season and final_pct > 0 and season.get("boost_now_pct"):
                boosted = int(min(100, round(final_pct * (1 + season["boost_now_pct"] / 100.0))))
                if boosted != final_pct:
                    notes.append(f"Winter lift +{season['boost_now_pct']:.0f}% ({season['day']['day_hours']:.1f} h of daylight)")
                final_pct = boosted
            if season and final_pct > 0 and season.get("kelvin_now") and working_color.mode == "kelvin" and working_color.kelvin:
                k = int(working_color.kelvin + season["kelvin_now"])
                working_color = working_color.model_copy(update={"kelvin": k, "rgb": kelvin_to_rgb(k)})

        roles_out[role] = RoleEvaluation(
            role=role,
            entities=entities,
            on=final_pct > 0,
            brightness_pct=final_pct,
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


def simulate_day(
    room_cfg: RoomConfig,
    schemas: dict[str, dict[str, Any]],
    date_ref: datetime,
    ambient_lux_override: float | None = None,
    tv_on_hhmm: str | None = "20:00",
    tv_off_hhmm: str | None = "22:30",
    desk_active: bool = False,
    punch_role: str | None = None,
    punch_delta: float = 0.0,
    punch_hhmm: str = "20:15",
    step_minutes: int = 15,
    learned_offsets: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Simulate 24 hours at `step_minutes` intervals for the HTTP Curve Sandbox."""
    day_start = date_ref.replace(hour=0, minute=0, second=0, microsecond=0)
    tv_on_m = _hhmm_or_none(tv_on_hhmm)
    tv_off_m = _hhmm_or_none(tv_off_hhmm)
    punch_m = _hhmm_or_none(punch_hhmm)

    points: list[dict[str, Any]] = []
    for mins in range(0, 1440, step_minutes):
        t = day_start + timedelta(minutes=mins)
        # Default synthetic daylight bell curve if user didn't pin ambient lux
        if ambient_lux_override is not None:
            lux = ambient_lux_override
        else:
            if 7 <= t.hour <= 18:
                # Peak 450 lux at 13:00
                lux = max(10.0, 450.0 * (1.0 - abs(mins - 780) / 390.0))
            else:
                lux = 5.0

        media_playing = False
        if tv_on_m is not None and tv_off_m is not None:
            media_playing, _ = time_in_window(mins, tv_on_m, tv_off_m)

        punches: dict[str, PunchState] = {}
        if punch_role and abs(punch_delta) > 0 and punch_m is not None and mins >= punch_m:
            punches[punch_role] = PunchState(
                role=punch_role,
                delta_pct=punch_delta,
                pushed_at=day_start + timedelta(minutes=punch_m),
                half_life_minutes=room_cfg.punch_half_life_minutes,
            )

        inputs = RoomInputs(
            occupied=True,
            ambient_lux=round(lux, 1),
            media_state="playing" if media_playing else "off",
            bedtime=(mins >= 23 * 60 + 30 or mins < 6 * 60),
            zones={z: (desk_active if "desk" in z else media_playing if "sofa" in z else False) for z in room_cfg.presence.zones},
        )
        mode_start = (
            day_start + timedelta(minutes=tv_on_m)
            if media_playing and tv_on_m is not None
            else day_start
        )
        ev = resolve_room(
            room_cfg,
            schemas,
            inputs,
            t,
            punches=punches,
            learned_offsets=learned_offsets,
            mode_entered_at=mode_start,
        )
        points.append({
            "time": t.strftime("%H:%M"),
            "ambient_lux": inputs.ambient_lux,
            "overlays": ev.active_overlays,
            "roles": {
                r: {
                    "on": rev.on,
                    "brightness_pct": rev.brightness_pct,
                    "rgb": rev.color.rgb,
                    "kelvin": rev.color.kelvin,
                    "stage": rev.color.stage,
                    "rule": rev.breakdown.rule_matched,
                }
                for r, rev in ev.roles.items()
            },
        })
    return points


def _hhmm_or_none(val: str | None) -> int | None:
    if not val or ":" not in val:
        return None
    h, m = val.strip().split(":", 1)
    return int(h) * 60 + int(m)
