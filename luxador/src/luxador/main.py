"""FastAPI application for El Luxador & Luxie (HTTP UI, 24h sandbox, hot-reload YAML, and telemetry)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .ha_bridge import HABridge
from .luxie import LuxieStore
from .models import PunchState, RoleEvaluation, RoomConfig, RoomInputs, SignalSpec
from .opnsense import OpnsensePoller
from .parser import load_config_bundle, load_room_yaml, load_schemas_yaml
from .presence import PresenceSnapshot, PresenceTracker
from .resolver import evaluate_overlay_condition, resolve_room, simulate_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("luxador.main")

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"

# Running as a Home Assistant app (add-on): Supervisor writes the Configuration-tab options here,
# mounts the app's own config folder at /config (addon_configs/<slug> on the host) and persistent
# storage at /data.
HA_OPTIONS_PATH = Path(os.environ.get("LUXADOR_HA_OPTIONS", "/data/options.json"))
IS_HA_APP = HA_OPTIONS_PATH.exists()
CONFIG_DIR = Path(os.environ.get("LUXADOR_CONFIG_DIR", "/config" if IS_HA_APP else str(BASE_DIR / "config")))
DEFAULT_CONFIG_DIR = BASE_DIR / "default_config"


def _seed_config_dir() -> None:
    """First start as an HA app: copy the example rooms/schemas/settings into the app's config folder."""
    if not DEFAULT_CONFIG_DIR.exists():
        return
    for src in DEFAULT_CONFIG_DIR.rglob("*"):
        dst = CONFIG_DIR / src.relative_to(DEFAULT_CONFIG_DIR)
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            logger.info("Seeded default config %s", dst)


def _ha_options() -> dict[str, Any]:
    if not IS_HA_APP:
        return {}
    try:
        return json.loads(HA_OPTIONS_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Could not read %s: %s", HA_OPTIONS_PATH, exc)
        return {}


def _load_settings() -> dict[str, Any]:
    path = CONFIG_DIR / "settings.yaml"
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}) or {}
    opn = dict(data.get("opnsense") or {})
    ha_opts = _ha_options()
    if ha_opts:
        # The HA Configuration tab wins over settings.yaml for the things it exposes
        for key in ("dry_run", "tick_seconds"):
            if key in ha_opts:
                data[key] = ha_opts[key]
        for opt_key, key in (("opnsense_url", "url"), ("opnsense_api_key", "api_key"),
                             ("opnsense_api_secret", "api_secret"), ("opnsense_verify_ssl", "verify_ssl")):
            if ha_opts.get(opt_key) not in (None, ""):
                opn[key] = ha_opts[opt_key]
    default_db = "/data/luxie.db" if IS_HA_APP else str(BASE_DIR / data.get("db_path", "data/luxie.db"))
    return {
        "ha_url": os.environ.get("LUXADOR_HA_URL", data.get("ha_url", "")),
        "ha_token": os.environ.get("LUXADOR_HA_TOKEN", data.get("ha_token", "")),
        "dry_run": os.environ.get("LUXADOR_DRY_RUN", str(data.get("dry_run", True))).lower() == "true",
        "tick_seconds": int(os.environ.get("LUXADOR_TICK_SECONDS", data.get("tick_seconds", 5))),
        "db_path": os.environ.get("LUXADOR_DB_PATH", default_db),
        "host": data.get("host", "0.0.0.0"),
        "port": int(os.environ.get("LUXADOR_PORT", data.get("port", 8099))),
        "presence_settle_seconds": float(data.get("presence_settle_seconds", 45)),
        "net_active_kbps": float(data.get("net_active_kbps", 20)),
        "opnsense": {
            "url": os.environ.get("LUXADOR_OPNSENSE_URL", opn.get("url", "")),
            "api_key": os.environ.get("LUXADOR_OPNSENSE_KEY", opn.get("api_key", "")),
            "api_secret": os.environ.get("LUXADOR_OPNSENSE_SECRET", opn.get("api_secret", "")),
            "verify_ssl": bool(opn.get("verify_ssl", True)),
            "interface": str(opn.get("interface", "lan")),
            "poll_seconds": float(opn.get("poll_seconds", 15)),
        },
    }


class EngineRuntime:
    """Holds live loaded configs, active punches, Luxie DB, and HA bridge."""

    def __init__(self) -> None:
        if IS_HA_APP:
            _seed_config_dir()
        self.settings = _load_settings()
        self.schemas, self.rooms = load_config_bundle(CONFIG_DIR)
        self.luxie = LuxieStore(self.settings["db_path"])
        self.punches: dict[str, dict[str, PunchState]] = {}
        self.sim_overrides: dict[str, RoomInputs] = {}
        self.last_commands: dict[str, list[dict[str, Any]]] = {}
        self.last_applied_bri: dict[tuple[str, str], int] = {}
        self.mode_entered_at: dict[str, datetime] = {}
        self.mode_key: dict[str, tuple] = {}
        self.overlay_weights: dict[str, dict[str, float]] = {}
        self.overlay_weights_at: dict[str, datetime] = {}
        self.presence = PresenceTracker(settle_seconds=self.settings["presence_settle_seconds"])
        self.presence_snap: dict[str, PresenceSnapshot] = {}
        self.last_rollup: datetime | None = None
        self.light_sample_last: dict[tuple[str, str], tuple[tuple, datetime]] = {}
        # Minute-grid accumulators for device <-> zone correlation
        self._pres_minute: dict[str, tuple[str, dict[str, bool]]] = {}
        self._net_minute: tuple[str, dict[str, tuple[str, float]]] | None = None
        self.net = OpnsensePoller(**self.settings["opnsense"])
        self.bridge = HABridge(
            ha_url=self.settings["ha_url"],
            ha_token=self.settings["ha_token"],
            dry_run=self.settings["dry_run"],
            on_external_light_change=self._handle_external_light_change,
        )
        # SANDBOX ONLY: a static snapshot of the Lounge entities captured once, so the UI has something to
        # show without HA. It is labelled as sandbox data everywhere and never written to the presence tables.
        for rname, rcfg in self.rooms.items():
            self.sim_overrides[rname] = RoomInputs(
                occupied=True,
                ambient_lux=439.0,
                media_state="off",
                bedtime=False,
                zones={z: False for z in rcfg.presence.zones},
                entity_states={
                    "light.living_room_threelight_l": {"state": "on", "attributes": {"brightness": 255, "color_temp_kelvin": 4000}},
                    "light.living_room_threelight_m": {"state": "on", "attributes": {"brightness": 255, "color_temp_kelvin": 4000}},
                    "light.living_room_threelight_t": {"state": "on", "attributes": {"brightness": 255, "color_temp_kelvin": 4000}},
                    "light.shoelight_light": {"state": "on", "attributes": {"brightness": 254, "color_temp_kelvin": 4000}},
                    "light.loungetv": {"state": "off", "attributes": {}},
                    "light.wled_strip": {"state": "off", "attributes": {}},
                    "light.kwn_desk": {"state": "unavailable", "attributes": {}},
                    "light.tz3000_46t1rvdu_ts0001": {"state": "off", "attributes": {}},
                },
                idle_minutes=0.0,
            )

    def _handle_external_light_change(
        self, entity_id: str, old_bri: int, new_bri: int, kind: str, actor: str, ctx_id: str = ""
    ) -> None:
        """Something other than Luxador changed one of our lights.

        Always recorded as a foreign write (-> 'Contested lights'). Only a *person* (HA user, or a physical
        switch/remote, which HA can't tell apart from an integration) becomes a punch. Automations are never
        learned from: that is another controller fighting us, and the fix is to turn it off, not to adapt.
        In dry-run nothing we computed is on the bulbs, so nothing is learned either.
        """
        now = datetime.now()
        for rname, rcfg in self.rooms.items():
            for role, entities in rcfg.bulbs.items():
                if entity_id not in entities:
                    continue
                self.luxie.record_foreign_write(entity_id, kind, actor, old_bri, new_bri, room=rname, context_id=ctx_id)
                expected = self.last_applied_bri.get((rname, role), 0)
                delta = float(new_bri - expected)
                if kind == "automation" or self.bridge.dry_run:
                    why = ("dry-run: bulbs aren't ours yet, not learned" if self.bridge.dry_run
                           else "another automation is driving this light; not learned, disable it for this light")
                    self.luxie.log_causal_event(rname, role, old_bri, new_bri, actor, f"Foreign change ({kind}): {why}",
                                                entity_id=entity_id, ts=now)
                    continue
                if abs(delta) >= 3:
                    self.apply_punch(rname, role, delta, actor=actor, now=now)

    def reload(self) -> None:
        self.schemas, self.rooms = load_config_bundle(CONFIG_DIR)

    @property
    def live(self) -> bool:
        return self.bridge.connected

    def effective_room(self, room_name: str) -> RoomConfig:
        """Room config plus any network devices you accepted as zone signals (no YAML edit needed)."""
        rcfg = self.rooms[room_name]
        learned = [s for s in self.luxie.get_learned_signals(room_name) if s["status"] == "accepted" and s["zone"]]
        if not learned:
            return rcfg
        zones = {z: zc.model_copy(deep=True) for z, zc in rcfg.presence.zones.items()}
        for s in learned:
            if s["zone"] not in zones:
                continue
            existing = {sig.host for sig in zones[s["zone"]].signals if sig.host}
            if s["host"] in existing:
                continue
            zones[s["zone"]].signals.append(SignalSpec(
                name=s["name"] or s["host"], source="opnsense", host=s["host"],
                active_kbps=s["active_kbps"] if s["active_kbps"] is not None else self.settings["net_active_kbps"],
            ))
        return rcfg.model_copy(update={"presence": rcfg.presence.model_copy(update={"zones": zones})})

    def _accumulate_minutes(self, room_name: str, snap: PresenceSnapshot, now: datetime) -> None:
        """Fold this tick into the current minute; flush the previous minute when it rolls over."""
        minute = now.strftime("%Y-%m-%dT%H:%M")
        occ = {"room": snap.occupied, **{z: zs.occupied for z, zs in snap.zones.items()}}
        cur = self._pres_minute.get(room_name)
        if cur and cur[0] != minute:
            self.luxie.record_presence_minute(cur[0], room_name, cur[1])
            cur = None
        if cur is None:
            self._pres_minute[room_name] = (minute, occ)
        else:
            for k, v in occ.items():
                cur[1][k] = cur[1].get(k, False) or v

    def _accumulate_net(self, now: datetime, floor_kbps: float = 5.0) -> None:
        if not self.net.ready:
            return
        minute = now.strftime("%Y-%m-%dT%H:%M")
        if self._net_minute and self._net_minute[0] != minute:
            if self._net_minute[1]:
                self.luxie.record_net_minute(*self._net_minute)
            self._net_minute = None
        if self._net_minute is None:
            self._net_minute = (minute, {})
        bucket = self._net_minute[1]
        for h in self.net.hosts:
            key = h.mac or h.ip
            if not key or h.kbps < floor_kbps:
                continue
            prev = bucket.get(key)
            bucket[key] = (h.hostname, max(h.kbps, prev[1] if prev else 0.0))

    def update_presence(self, room_name: str, now: datetime) -> PresenceSnapshot:
        """Advance the ROOM > PRESENCE > ZONE tracker; persist signal changes/episodes only from live data."""
        rcfg = self.effective_room(room_name)
        if self.live:
            upd = self.presence.update(rcfg, self.bridge.states, self.net, now)
        else:
            sim = self.sim_overrides.get(room_name, RoomInputs())
            sim_states = dict(sim.entity_states)
            if rcfg.media_sensor:  # let the sandbox TV dropdown drive any signal bound to the media player
                sim_states[rcfg.media_sensor] = {"state": sim.media_state}
            upd = self.presence.update(
                rcfg, sim_states, self.net, now,
                zone_overrides=sim.zones, occupied_override=sim.occupied, hold=False, settle_seconds=0,
            )
        upd.snapshot.recorded = self.live
        if self.live:
            for sig in upd.signal_changes:
                self.luxie.record_signal_event(room_name, sig.scope, sig.name, sig.source, sig.active,
                                               sig.available, sig.value, ts=now)
            if upd.closed_episode:
                sig_s, start, end = upd.closed_episode
                self.luxie.record_episode(room_name, sig_s, start, end)
            self._accumulate_minutes(room_name, upd.snapshot, now)
        self.presence_snap[room_name] = upd.snapshot
        return upd.snapshot

    def current_inputs(self, room_name: str) -> RoomInputs:
        rcfg = self.rooms[room_name]
        snap = self.presence_snap.get(room_name)
        if snap is None:
            snap = self.update_presence(room_name, datetime.now())
        learned, match = self.luxie.resolve_state(room_name, snap.signature)
        if learned == "empty" and snap.occupied:
            learned, match = None, None  # someone just arrived; the 'empty' pattern is still settling out
        zones = {z: zs.occupied for z, zs in snap.zones.items()}

        if not self.live:
            sim = self.sim_overrides.get(room_name, RoomInputs())
            return sim.model_copy(update={
                "zones": {**zones, **sim.zones},
                "signature": snap.signature,
                "learned_state": learned,
                "learned_match": match,
                "lux_source": "sandbox",
                "warnings": ["SANDBOX: Home Assistant not connected; inputs are a static snapshot / your test values"],
            })

        states = self.bridge.states
        warnings: list[str] = []
        lux_source = "sensor"
        lux_raw = states.get(rcfg.lux_sensor or "", {}).get("state")
        try:
            lux = float(lux_raw)
        except (TypeError, ValueError):
            lux = 120.0
            lux_source = "fallback"
            warnings.append(f"Lux sensor {rcfg.lux_sensor} is {lux_raw!r}; using fallback 120 lux (target_lux is guessing)")
        media = states.get(rcfg.media_sensor or "", {}).get("state", "off")
        bedtime = bool(rcfg.bedtime_entity) and states.get(rcfg.bedtime_entity or "", {}).get("state") == "on"
        unavailable = [s.key for s in snap.signals if not s.available]
        if unavailable:
            warnings.append(f"Signals unavailable: {', '.join(unavailable)}")
        return RoomInputs(
            occupied=snap.occupied,
            ambient_lux=lux,
            media_state=media,
            bedtime=bedtime,
            zones=zones,
            entity_states=states,
            idle_minutes=snap.idle_minutes,
            signature=snap.signature,
            learned_state=learned,
            learned_match=match,
            lux_source=lux_source,
            warnings=warnings,
        )

    def step_overlay_weights(self, room_name: str, inputs: RoomInputs, now: datetime) -> dict[str, float]:
        """Ease each overlay's weight toward 1 (condition true) or 0 over its blend_minutes."""
        rcfg = self.rooms[room_name]
        weights = self.overlay_weights.setdefault(room_name, {})
        last = self.overlay_weights_at.get(room_name)
        dt = (now - last).total_seconds() if last else None
        self.overlay_weights_at[room_name] = now
        for ov in rcfg.overlays:
            target = 1.0 if evaluate_overlay_condition(ov.when, inputs, now) else 0.0
            cur = weights.get(ov.name)
            if cur is None or ov.blend_minutes <= 0:
                # First sight (startup) or blending disabled: start where we are, don't fade in from scratch
                weights[ov.name] = target
                continue
            if dt is None:
                continue
            step = max(0.0, dt) / (ov.blend_minutes * 60.0)
            weights[ov.name] = min(target, cur + step) if target > cur else max(target, cur - step)
        return dict(weights)

    def evaluate_room(self, room_name: str, now: datetime | None = None) -> dict[str, Any]:
        if room_name not in self.rooms:
            raise KeyError(room_name)
        now = now or datetime.now()
        rcfg = self.rooms[room_name]
        # Merge learned empirical lux gains into room copy if present
        learned_gains = self.luxie.get_bulb_lux_gains(room_name)
        if learned_gains:
            rcfg = rcfg.model_copy(update={"bulb_lux_gain": {**rcfg.bulb_lux_gain, **learned_gains}})

        inputs = self.current_inputs(room_name)
        learned_offsets = self.luxie.get_learned_offsets(room_name)
        weights = self.overlay_weights.get(room_name)
        ev = resolve_room(
            rcfg,
            self.schemas,
            inputs,
            now,
            punches=self.punches.get(room_name),
            learned_offsets=learned_offsets,
            mode_entered_at=self.mode_entered_at.get(room_name),
            overlay_weights=weights,
        )
        drift = self.luxie.check_lux_drift(room_name, inputs.ambient_lux, now)
        return {
            "evaluation": ev.model_dump(mode="json"),
            "inputs": inputs.model_dump(mode="json"),
            "lux_drift": drift,
            "dry_run": self.bridge.dry_run,
            "ha_connected": self.bridge.connected,
            "overlay_weights": {k: round(v, 2) for k, v in (weights or {}).items()},
            "presence": self.presence_snap[room_name].to_dict() if room_name in self.presence_snap else None,
            "last_commands": self.last_commands.get(room_name, []),
            "room_config": {
                "friendly_name": rcfg.friendly_name,
                "presence": rcfg.presence.model_dump(mode="json"),
                "bedtime_entity": rcfg.bedtime_entity,
                "lux_sensor": rcfg.lux_sensor,
                "media_sensor": rcfg.media_sensor,
                "traits": rcfg.traits,
                "tweaks": rcfg.tweaks,
                "bulb_lux_gain": rcfg.bulb_lux_gain,
                "punch_half_life_minutes": rcfg.punch_half_life_minutes,
                "rules": [r.raw for r in rcfg.rules],
                "overlays": [
                    {
                        "name": o.name,
                        "when": o.when,
                        "priority": o.priority,
                        "time_warp_minutes": o.time_warp_minutes,
                        "blend_minutes": o.blend_minutes,
                        "rules": [r.raw for r in o.rules],
                    }
                    for o in rcfg.overlays
                ],
            },
            "schemas": self.schemas,
        }

    def apply_punch(
        self,
        room_name: str,
        role: str,
        delta_pct: float,
        actor: str = "ui_test",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now()
        rcfg = self.rooms[room_name]
        ev_before = self.evaluate_room(room_name, now=now)["evaluation"]
        role_before = ev_before["roles"].get(role)
        from_bri = role_before["brightness_pct"] if role_before else 0
        bucket_key = role_before["breakdown"]["bucket_key"] if role_before else "default"

        existing = self.punches.setdefault(room_name, {}).get(role)
        # Accumulate on top of active punch if any
        total_delta = delta_pct + (existing.delta_pct if existing else 0.0)
        self.punches[room_name][role] = PunchState(
            role=role,
            delta_pct=total_delta,
            pushed_at=now,
            half_life_minutes=rcfg.punch_half_life_minutes,
            actor=actor,
        )

        learn_info = self.luxie.record_user_punch(room_name, role, bucket_key, delta_pct)
        ev_after = self.evaluate_room(room_name, now=now)["evaluation"]
        role_after = ev_after["roles"].get(role)
        to_bri = role_after["brightness_pct"] if role_after else from_bri

        self.luxie.log_causal_event(
            room=room_name,
            role=role,
            from_bri=from_bri,
            to_bri=to_bri,
            actor=actor,
            reason=f"User punched {delta_pct:+.0f}% (punching-bag active; overrides in bucket={learn_info['override_count']})",
            breakdown=role_after["breakdown"] if role_after else {},
            ts=now,
        )
        return {"before": from_bri, "after": to_bri, "learning": learn_info}

    def _sample_lights(self, room_name: str, inputs: dict[str, Any], ev: dict[str, Any], now: datetime) -> None:
        """Record what each role's bulbs actually are, on change or every 5 minutes (Phase 3 training data)."""
        rcfg = self.rooms[room_name]
        for role, entities in rcfg.bulbs.items():
            on, bri, kelvin, rgb = False, None, None, None
            for e in entities:
                st = self.bridge.states.get(e, {})
                if st.get("state") != "on":
                    continue
                attrs = st.get("attributes") or {}
                on = True
                b = attrs.get("brightness")
                bri = max(bri or 0, int(round(float(b) / 2.55)) if b is not None else 100)
                kelvin = kelvin or attrs.get("color_temp_kelvin")
                rgb = rgb or attrs.get("rgb_color")
            fp = (on, bri, kelvin, tuple(rgb) if rgb else None, inputs.get("signature"))
            last = self.light_sample_last.get((room_name, role))
            if last and last[0] == fp and (now - last[1]).total_seconds() < 300:
                continue
            self.light_sample_last[(room_name, role)] = (fp, now)
            target = ev["roles"].get(role, {}).get("brightness_pct")
            punched = role in self.punches.get(room_name, {})
            self.luxie.record_light_sample(
                room_name, role, inputs.get("signature") or "", inputs.get("learned_state"), on, bri,
                int(kelvin) if kelvin else None, rgb, target, inputs.get("ambient_lux"), punched, ts=now,
            )

    async def tick_once(self) -> None:
        now = datetime.now()
        self._accumulate_net(now)
        for rname in list(self.rooms):
            self.update_presence(rname, now)
            self.step_overlay_weights(rname, self.current_inputs(rname), now)
            data = self.evaluate_room(rname, now=now)
            ev = data["evaluation"]
            inputs = data["inputs"]

            # Animated schemas run off time-in-mode: restart the clock when the overlay set / state changes
            key = (tuple(ev["active_overlays"]), inputs.get("learned_state"))
            if self.mode_key.get(rname) != key:
                self.mode_key[rname] = key
                self.mode_entered_at[rname] = now

            if self.live:  # sandbox ticks are not telemetry
                self.luxie.record_raw_tick(
                    room=rname,
                    ambient_lux=inputs["ambient_lux"],
                    occupied=inputs["occupied"],
                    media_state=inputs["media_state"],
                    inferred_state=ev["confidence"]["inferred_state"],
                    payload={
                        "signature": inputs.get("signature"),
                        "learned_state": inputs.get("learned_state"),
                        "lux_source": inputs.get("lux_source"),
                        "overlay_weights": data.get("overlay_weights"),
                    },
                    ts=now,
                )
                self._sample_lights(rname, inputs, ev, now)
            cmds: list[dict[str, Any]] = []
            for role, r_dict in ev["roles"].items():
                prev_bri = self.last_applied_bri.get((rname, role))
                new_bri = r_dict["brightness_pct"]
                if prev_bri != new_bri:
                    self.luxie.log_causal_event(
                        room=rname,
                        role=role,
                        from_bri=prev_bri if prev_bri is not None else new_bri,
                        to_bri=new_bri,
                        actor="luxador_engine",
                        reason=r_dict["breakdown"]["rule_matched"],
                        breakdown=r_dict["breakdown"],
                        ts=now,
                    )
                    self.last_applied_bri[(rname, role)] = new_bri
                rev_obj = RoleEvaluation.model_validate(r_dict)
                cmds.extend(await self.bridge.apply_role(rev_obj))
            self.last_commands[rname] = cmds


runtime: EngineRuntime | None = None


async def _background_loop() -> None:
    assert runtime is not None
    while True:
        try:
            await runtime.tick_once()
            now = datetime.now()
            if runtime.last_rollup is None or (now - runtime.last_rollup).total_seconds() >= 3600:
                runtime.last_rollup = now
                runtime.luxie.run_rollup()
                runtime.luxie.prune_minutes()
        except Exception as exc:
            logger.exception("Engine tick error: %s", exc)
        await asyncio.sleep(runtime.settings["tick_seconds"])


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global runtime
    runtime = EngineRuntime()
    ws_task = asyncio.create_task(runtime.bridge.run_ws_loop())
    tick_task = asyncio.create_task(_background_loop())
    net_task = asyncio.create_task(runtime.net.run())
    try:
        yield
    finally:
        ws_task.cancel()
        tick_task.cancel()
        net_task.cancel()
        runtime.luxie.close()


app = FastAPI(title="El Luxador & Luxie", version="0.1.0", lifespan=lifespan)


class PunchRequest(BaseModel):
    role: str
    delta_pct: float
    actor: str = "manual_ui"


class SimulateDayRequest(BaseModel):
    ambient_lux: float | None = None
    tv_on: str | None = "19:30"
    tv_off: str | None = "22:30"
    desk_active: bool = False
    punch_role: str | None = None
    punch_delta: float = 0.0
    punch_time: str = "20:15"


class YamlSaveRequest(BaseModel):
    content: str


class ConfirmStateRequest(BaseModel):
    suggested_state: str
    user_label: str
    confidence: float


class PatternLabelRequest(BaseModel):
    signature: str
    label: str | None = None
    ignored: bool = False


class DeviceDecisionRequest(BaseModel):
    host: str
    status: str  # accepted | ignored | clear
    zone: str | None = None
    name: str | None = None
    active_kbps: float | None = None


class OffsetActionRequest(BaseModel):
    role: str
    bucket_key: str
    frozen: bool | None = None


class RuleMutationRequest(BaseModel):
    rule: str | None = None
    index: int | None = None


class TweaksUpdateRequest(BaseModel):
    punch_half_life_minutes: float | None = None
    clear_delay_seconds: int | None = None
    tweaks: dict[str, Any] = {}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    html_path = WEB_DIR / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/api/overview")
async def get_overview() -> dict[str, Any]:
    assert runtime is not None
    now = datetime.now()
    room_cards = []
    for rname in runtime.rooms:
        st = runtime.evaluate_room(rname, now=now)
        ev = st["evaluation"]
        inp = st["inputs"]
        active_roles = sum(1 for r in ev["roles"].values() if r["on"])
        room_cards.append({
            "room": rname,
            "friendly_name": st["room_config"]["friendly_name"],
            "occupied": inp["occupied"],
            "ambient_lux": inp["ambient_lux"],
            "media_state": inp["media_state"],
            "inferred_state": ev["confidence"]["inferred_state"],
            "active_overlays": ev["active_overlays"],
            "state_source": ev["confidence"].get("state_source"),
            "signature": inp.get("signature"),
            "warnings": inp.get("warnings", []),
            "active_roles": active_roles,
            "total_roles": len(ev["roles"]),
            "lux_drift": st["lux_drift"],
        })
    return {
        "house": {
            "implemented": False,
            "note": "House-level presence/mode is not built yet (per-room presence only)",
        },
        "opnsense": runtime.net.status(),
        "rooms": room_cards,
        "dry_run": runtime.bridge.dry_run,
        "ha_connected": runtime.bridge.connected,
    }


@app.get("/api/rooms")
async def list_rooms() -> dict[str, Any]:
    assert runtime is not None
    return {
        "rooms": [
            {"room": r.room, "friendly_name": r.friendly_name}
            for r in runtime.rooms.values()
        ],
        "dry_run": runtime.bridge.dry_run,
        "ha_connected": runtime.bridge.connected,
    }


@app.get("/api/rooms/{room_name}/state")
async def get_room_state(room_name: str, time_override: str | None = None) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    now = datetime.now()
    if time_override and ":" in time_override:
        h, m = time_override.split(":", 1)
        now = now.replace(hour=int(h), minute=int(m), second=0)
    return runtime.evaluate_room(room_name, now=now)


@app.post("/api/rooms/{room_name}/inputs")
async def set_sim_inputs(room_name: str, inputs: RoomInputs) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    runtime.sim_overrides[room_name] = inputs
    await runtime.tick_once()
    return runtime.evaluate_room(room_name)


@app.post("/api/rooms/{room_name}/punch")
async def punch_role(room_name: str, req: PunchRequest) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    return runtime.apply_punch(room_name, req.role, req.delta_pct, actor=req.actor)


@app.post("/api/rooms/{room_name}/simulate-day")
async def simulate_room_day(room_name: str, req: SimulateDayRequest) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    rcfg = runtime.rooms[room_name]
    learned = runtime.luxie.get_learned_offsets(room_name)
    points = simulate_day(
        rcfg,
        runtime.schemas,
        datetime.now(),
        ambient_lux_override=req.ambient_lux,
        tv_on_hhmm=req.tv_on,
        tv_off_hhmm=req.tv_off,
        desk_active=req.desk_active,
        punch_role=req.punch_role,
        punch_delta=req.punch_delta,
        punch_hhmm=req.punch_time,
        learned_offsets=learned,
    )
    return {"room": room_name, "roles": list(rcfg.bulbs.keys()), "points": points}


@app.get("/api/yaml/room/{room_name}")
async def get_room_yaml(room_name: str) -> dict[str, str]:
    path = CONFIG_DIR / "rooms" / f"{room_name}.yaml"
    if not path.exists():
        raise HTTPException(404, "Room YAML not found")
    return {"content": path.read_text(encoding="utf-8")}


@app.post("/api/yaml/room/{room_name}")
async def save_room_yaml(room_name: str, req: YamlSaveRequest) -> dict[str, Any]:
    assert runtime is not None
    try:
        parsed: RoomConfig = load_room_yaml(req.content)
    except Exception as exc:
        raise HTTPException(400, f"YAML validation error: {exc}") from exc
    path = CONFIG_DIR / "rooms" / f"{room_name}.yaml"
    path.write_text(req.content, encoding="utf-8")
    runtime.reload()
    return {"ok": True, "room": parsed.room, "rules_count": len(parsed.rules)}


@app.get("/api/yaml/schemas")
async def get_schemas_yaml() -> dict[str, str]:
    path = CONFIG_DIR / "schemas.yaml"
    return {"content": path.read_text(encoding="utf-8")}


@app.post("/api/yaml/schemas")
async def save_schemas_yaml(req: YamlSaveRequest) -> dict[str, Any]:
    assert runtime is not None
    try:
        parsed = load_schemas_yaml(req.content)
    except Exception as exc:
        raise HTTPException(400, f"Schemas YAML validation error: {exc}") from exc
    (CONFIG_DIR / "schemas.yaml").write_text(req.content, encoding="utf-8")
    runtime.reload()
    return {"ok": True, "schemas": list(parsed.keys())}


@app.get("/api/luxie/{room_name}/logs")
async def get_luxie_logs(room_name: str, limit: int = 50) -> dict[str, Any]:
    assert runtime is not None
    return {
        "causal_logs": runtime.luxie.get_causal_logs(room_name, limit=limit),
        "offsets": runtime.luxie.list_baseline_offsets(room_name),
        "confirmations": runtime.luxie.get_confirmations(room_name),
        "telemetry": runtime.luxie.get_telemetry_summary(room_name),
    }


@app.post("/api/luxie/{room_name}/confirm")
async def confirm_room_state(room_name: str, req: ConfirmStateRequest) -> dict[str, Any]:
    assert runtime is not None
    runtime.luxie.record_confirmation(room_name, req.suggested_state, req.user_label, req.confidence)
    if req.user_label == "Sleeping" and room_name in runtime.sim_overrides:
        runtime.sim_overrides[room_name].bedtime = True
    return {"ok": True}


@app.post("/api/luxie/{room_name}/rollup")
async def trigger_luxie_rollup(room_name: str) -> dict[str, Any]:
    assert runtime is not None
    stats = runtime.luxie.run_rollup()
    return {"ok": True, "stats": stats}


@app.post("/api/luxie/{room_name}/offset/reset")
async def reset_luxie_offset(room_name: str, req: OffsetActionRequest) -> dict[str, Any]:
    assert runtime is not None
    runtime.luxie.reset_offset(room_name, req.role, req.bucket_key)
    return {"ok": True}


@app.post("/api/luxie/{room_name}/offset/freeze")
async def freeze_luxie_offset(room_name: str, req: OffsetActionRequest) -> dict[str, Any]:
    assert runtime is not None
    runtime.luxie.set_offset_frozen(room_name, req.role, req.bucket_key, bool(req.frozen))
    return {"ok": True}


@app.get("/api/presence/{room_name}")
async def get_presence(room_name: str) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    snap = runtime.presence_snap.get(room_name) or runtime.update_presence(room_name, datetime.now())
    learned, match = runtime.luxie.resolve_state(room_name, snap.signature)
    return {
        "snapshot": snap.to_dict(),
        "learned_state": learned,
        "learned_match": match,
        "recording": runtime.live,
        "patterns": runtime.luxie.pattern_summary(room_name),
        "signal_health": runtime.luxie.signal_health(room_name),
        "opnsense": runtime.net.status(),
    }


@app.post("/api/presence/{room_name}/label")
async def label_pattern(room_name: str, req: PatternLabelRequest) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    if req.signature == "":
        raise HTTPException(400, "The empty pattern is always 'empty'")
    runtime.luxie.set_pattern_label(room_name, req.signature, req.label, ignored=req.ignored)
    return {"ok": True, "patterns": runtime.luxie.pattern_summary(room_name)}


@app.get("/api/rooms/{room_name}/contested")
async def get_contested(room_name: str, hours: float = 24.0) -> dict[str, Any]:
    assert runtime is not None
    return {"lights": runtime.luxie.contested_lights(room_name, hours=hours)}


@app.get("/api/learn/{room_name}/weekly")
async def get_weekly_profile(room_name: str, days: int = 7) -> dict[str, Any]:
    """Phase 3 preview (read-only): what the last week of real light levels says, per state/role/hour."""
    assert runtime is not None
    return runtime.luxie.weekly_light_profile(room_name, days=days)


@app.get("/api/learn/{room_name}/devices")
async def get_device_associations(room_name: str, days: int = 7) -> dict[str, Any]:
    """Which network devices move with which zones (learned from the minute grid)."""
    assert runtime is not None
    data = runtime.luxie.net_zone_associations(room_name, days=days, active_kbps=runtime.settings["net_active_kbps"])
    data["opnsense"] = runtime.net.status()
    return data


@app.post("/api/learn/{room_name}/devices")
async def decide_device(room_name: str, req: DeviceDecisionRequest) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    if req.status not in ("accepted", "ignored", "clear"):
        raise HTTPException(400, "status must be accepted, ignored or clear")
    if req.status == "accepted" and req.zone not in runtime.rooms[room_name].presence.zones:
        raise HTTPException(400, f"Unknown zone: {req.zone}")
    name = req.name
    if req.status == "accepted" and not name:
        name = re.sub(r"[^a-z0-9_]+", "_", (req.host.split(".")[0]).lower()).strip("_") or "device"
    runtime.luxie.set_learned_signal(room_name, req.host, req.status, zone=req.zone, name=name,
                                     active_kbps=req.active_kbps)
    runtime.update_presence(room_name, datetime.now())  # show the new signal straight away
    return {"ok": True}


@app.get("/api/net/hosts")
async def get_net_hosts() -> dict[str, Any]:
    assert runtime is not None
    hosts = sorted(runtime.net.hosts, key=lambda h: -h.kbps)
    return {"status": runtime.net.status(), "hosts": [h.to_dict() for h in hosts]}


def _update_room_yaml_dict(room_name: str, mutator) -> RoomConfig:
    path = CONFIG_DIR / "rooms" / f"{room_name}.yaml"
    if not path.exists():
        raise HTTPException(404, f"Room YAML not found: {room_name}")
    raw_text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}
    mutator(data)
    updated_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    parsed = load_room_yaml(updated_text)
    path.write_text(updated_text, encoding="utf-8")
    assert runtime is not None
    runtime.reload()
    return parsed


@app.post("/api/rooms/{room_name}/rules/add")
async def add_room_rule(room_name: str, req: RuleMutationRequest) -> dict[str, Any]:
    if not req.rule or not req.rule.strip():
        raise HTTPException(400, "Missing rule string")
    try:
        parsed = _update_room_yaml_dict(
            room_name,
            lambda d: d.setdefault("rules", []).append(req.rule.strip()),
        )
    except Exception as exc:
        raise HTTPException(400, f"Invalid rule: {exc}") from exc
    return {"ok": True, "rules": [r.raw for r in parsed.rules]}


@app.post("/api/rooms/{room_name}/rules/remove")
async def remove_room_rule(room_name: str, req: RuleMutationRequest) -> dict[str, Any]:
    def _rm(d: dict[str, Any]) -> None:
        rules = d.get("rules", [])
        if req.index is None or not (0 <= req.index < len(rules)):
            raise ValueError("Rule index out of range")
        rules.pop(req.index)

    try:
        parsed = _update_room_yaml_dict(room_name, _rm)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "rules": [r.raw for r in parsed.rules]}


@app.post("/api/rooms/{room_name}/tweaks")
async def update_room_tweaks(room_name: str, req: TweaksUpdateRequest) -> dict[str, Any]:
    def _mutate(d: dict[str, Any]) -> None:
        if req.punch_half_life_minutes is not None:
            d["punch_half_life_minutes"] = float(req.punch_half_life_minutes)
        if req.clear_delay_seconds is not None:
            d.setdefault("presence", {})["clear_delay_seconds"] = int(req.clear_delay_seconds)
        if req.tweaks:
            d.setdefault("tweaks", {}).update(req.tweaks)

    parsed = _update_room_yaml_dict(room_name, _mutate)
    return {"ok": True, "tweaks": parsed.tweaks, "punch_half_life_minutes": parsed.punch_half_life_minutes}


def run() -> None:
    settings = _load_settings()
    uvicorn.run("luxador.main:app", host=settings["host"], port=settings["port"], reload=False)


if __name__ == "__main__":
    run()
