"""FastAPI application for El Luxador & Luxie (HTTP UI, 24h sandbox, hot-reload YAML, and telemetry)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .datalab import RANGES, DataLab
from .resolver import standard_darkness
from .seasonal import sun_elevation
from .standard import (
    SOFT_START_SECONDS,
    STANDARD_VERSION,
    HouseModel,
    LightSettings,
    build_house,
    curve_points,
    deep_merge,
    locked_hits,
    paths_in,
    suggest_thresholds,
)
from .seasonal import LIMITS as SEASONAL_LIMITS
from .seasonal import SeasonalConfig, SeasonalModel, clean_overrides
from .datalab import add_since_active as d_add_since_active
from .datalab import series as d_series
from .ha_bridge import HABridge
from .house import house_overview
from .luxie import LuxieStore
from .models import PunchState, RoleEvaluation, RoomConfig, RoomInputs, SignalSpec
from .opnsense import OpnsensePoller
from .parser import load_config_bundle, load_room_yaml, load_schemas_yaml
from .presence import PresenceSnapshot, PresenceTracker
from .resolver import evaluate_overlay_condition, resolve_room

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
        "outdoor_lux_sensor": str(data.get("outdoor_lux_sensor") or ""),
        "sun_entity": str(data.get("sun_entity") or "sun.sun"),
        "time_zone": str(data.get("time_zone") or ""),
        "max_states": int(data.get("max_states", 28) or 28),
        "detail_days": int(data.get("detail_days", 90) or 90),
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "seasonal": dict(data.get("seasonal") or {}),
        "curve": dict(data.get("curve") or {}),
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
        # Minute-grid accumulators for device <-> zone correlation
        self._pres_minute: dict[str, tuple[str, dict[str, bool]]] = {}
        self._net_minute: tuple[str, dict[str, tuple[str, float]]] | None = None
        self.net = OpnsensePoller(**self.settings["opnsense"])
        self.bridge = HABridge(
            ha_url=self.settings["ha_url"],
            ha_token=self.settings["ha_token"],
            dry_run=self.settings["dry_run"],
            on_external_light_change=self._handle_external_light_change,
            on_state_change=lambda e, st: self.datalab.capture(e, st),
        )
        self.datalab = DataLab(self.luxie, self.rooms, self.settings, self.bridge)
        self.last_active_at: dict[str, datetime] = {}
        self._live_state_cache: dict[str, dict[str, Any] | None] = {}
        self.last_datalab_rebuild: datetime | None = None
        self.darkness_ema: dict[str, tuple[float, datetime]] = {}
        self.on_dates: dict[tuple[str, str], str] = {}
        self.last_vacuum: datetime | None = None
        # Not connected to Home Assistant: inputs are "nothing known" (empty room, no lux), never made-up numbers.
        # Tests and development can still post inputs to /api/rooms/{room}/inputs.
        for rname, rcfg in self.rooms.items():
            self.sim_overrides[rname] = RoomInputs(occupied=False, ambient_lux=0.0, zones={z: False for z in rcfg.presence.zones})

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
        self.datalab.rooms = self.rooms

    def _track_activity(self, room_name: str, inputs: dict[str, Any], ev: dict[str, Any], now: datetime) -> None:
        lights_on = any(
            self.bridge.states.get(e, {}).get("state") == "on"
            for ents in self.rooms[room_name].bulbs.values() for e in ents
        )
        if inputs.get("occupied") or lights_on or inputs.get("media_state") in ("playing", "buffering"):
            self.last_active_at[room_name] = now

    def seasonal_model(self) -> SeasonalModel:
        lat = self.settings.get("latitude")
        lon = self.settings.get("longitude")
        if lat in (None, "") or lon in (None, ""):
            lat, lon = self.bridge.latitude, self.bridge.longitude
        tz_name = self.settings.get("time_zone") or self.bridge.time_zone or "UTC"
        return SeasonalModel(float(lat) if lat not in (None, "") else None,
                             float(lon) if lon not in (None, "") else None,
                             tz_name, SeasonalConfig.from_settings(self.seasonal_settings()))

    def seasonal_overrides(self) -> dict[str, Any]:
        m = self.luxie.load_model("_house", "seasonal_overrides")
        return json.loads(m[1]) if m else {}

    # ---------------------------------------------------------------- the house model (Standard)
    def house_overrides(self) -> dict[str, Any]:
        m = self.luxie.load_model("_house", "house_overrides")
        return json.loads(m[1]) if m else {}

    def house_locks(self) -> list[str]:
        m = self.luxie.load_model("_house", "house_locks")
        return json.loads(m[1]) if m else []

    def house(self) -> tuple[HouseModel | None, list[str]]:
        return build_house(self.settings, self.rooms, self.house_overrides())

    def house_model(self) -> HouseModel | None:
        model, errors = self.house()
        if errors:
            logger.warning("House model invalid (%s); rooms fall back to their legacy rules", "; ".join(errors))
        return model

    def sun_now(self, now: datetime) -> float | None:
        if self.live:
            return ((self.bridge.states.get(self.settings.get("sun_entity") or "sun.sun") or {})
                    .get("attributes") or {}).get("elevation")
        model = self.seasonal_model()
        if not model.located:
            return None
        local = now if now.tzinfo else now.replace(tzinfo=self.datalab.tz())
        return round(sun_elevation(model.lat, model.lon, local), 1)

    def step_darkness(self, room_name: str, now: datetime) -> None:
        """Stage 2 smoothing: an exponential average with a ~5 min time constant (no flicker on passing clouds)."""
        house = self.house_model()
        if not house or room_name not in house.rooms:
            return
        inp = self.current_inputs(room_name, smoothed=False)
        raw, _ = standard_darkness(house, self.rooms[room_name], inp)
        prev = self.darkness_ema.get(room_name)
        if prev is None or not self.live:
            self.darkness_ema[room_name] = (raw, now)  # sandbox: follow the slider immediately
            return
        dt = max(0.0, (now - prev[1]).total_seconds())
        a = min(1.0, dt / 300.0)
        self.darkness_ema[room_name] = (prev[0] + (raw - prev[0]) * a, now)

    def seasonal_locks(self) -> list[str]:
        m = self.luxie.load_model("_house", "seasonal_locks")
        return json.loads(m[1]) if m else []

    def seasonal_settings(self) -> dict[str, Any]:
        """settings.yaml, with anything tuned live in the UI layered on top."""
        return {**(self.settings.get("seasonal") or {}), **self.seasonal_overrides()}

    def seasonal_now(self, now: datetime) -> dict[str, Any]:
        model = self.seasonal_model()
        local = now.astimezone(self.datalab.tz()) if now.tzinfo else now.replace(tzinfo=self.datalab.tz())
        return model.now(local)

    def soft_start_seconds(self, room_name: str, role: str, prev: int | None, new: int, now: datetime) -> int:
        """Standard §4 stage 9: fade in from off per the light's soft_start setting."""
        from .standard import DEFAULT_TRANSITION_S

        key, today = (room_name, role), now.strftime("%Y-%m-%d")
        first_today = self.on_dates.get(key) != today
        if new > 0:
            self.on_dates[key] = today
        if not (new > 0 and not prev):
            return DEFAULT_TRANSITION_S
        house = self.house_model()
        rm = house.rooms.get(room_name) if house else None
        ls = (rm.lights.get(role) if rm else None) or LightSettings()
        if ls.soft_start == "always" or (ls.soft_start == "first_of_day" and first_today):
            return SOFT_START_SECONDS[ls.soft_start_speed]
        return DEFAULT_TRANSITION_S

    def current_pins(self, room_name: str) -> dict[str, dict[str, Any]]:
        """Switchboard pins for the learned pattern the room is in right now."""
        return (self.live_state(room_name) or {}).get("links") or {}

    def live_state(self, room_name: str) -> dict[str, Any] | None:
        """The learned pattern (discovered state) the room is in right now, or None."""
        if room_name not in self._live_state_cache:
            self._current_inputs(room_name)
        return self._live_state_cache.get(room_name)

    def _compute_live_state(self, room_name: str, occupied: bool, media: str, zones: dict[str, bool]) -> dict[str, Any] | None:
        last = self.last_active_at.get(room_name)
        since = (datetime.now() - last).total_seconds() / 60.0 if last else None
        try:
            st = self.datalab.live_state(room_name, occupied, media in ("playing", "buffering"), zones, since)
        except Exception as exc:  # a broken learned model must never stop the lights
            logger.warning("live state failed for %s: %s", room_name, exc)
            st = None
        self._live_state_cache[room_name] = st
        return st

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
            self._accumulate_minutes(room_name, upd.snapshot, now)
        self.presence_snap[room_name] = upd.snapshot
        return upd.snapshot

    def current_inputs(self, room_name: str, smoothed: bool = True) -> RoomInputs:
        inp = self._current_inputs(room_name)
        ema = self.darkness_ema.get(room_name) if smoothed else None
        return inp.model_copy(update={"sun_elev": self.sun_now(datetime.now()),
                                      "darkness": round(ema[0], 4) if ema else None})

    def _current_inputs(self, room_name: str) -> RoomInputs:
        rcfg = self.rooms[room_name]
        snap = self.presence_snap.get(room_name)
        if snap is None:
            snap = self.update_presence(room_name, datetime.now())
        zones = {z: zs.occupied for z, zs in snap.zones.items()}

        if not self.live:
            sim = self.sim_overrides.get(room_name, RoomInputs())
            zmap = {**zones, **sim.zones}
            st = self._compute_live_state(room_name, sim.occupied, sim.media_state, zmap)
            return sim.model_copy(update={
                "zones": zmap,
                "signature": snap.signature,
                "learned_state": (st or {}).get("intent"),
                "state_name": (st or {}).get("name"),
                "lux_source": "sandbox",
                "warnings": ["Not connected to Home Assistant: nothing is being read or controlled."],
            })

        states = self.bridge.states
        warnings: list[str] = []
        lux_source = "sensor"
        lux_raw = states.get(rcfg.lux_sensor or "", {}).get("state")
        try:
            lux = float(lux_raw)
        except (TypeError, ValueError):
            lux = 0.0
            lux_source = "unavailable"
            warnings.append(f"Lux sensor {rcfg.lux_sensor or '(none)'} is {lux_raw!r}: darkness comes from the sun instead")
        media = states.get(rcfg.media_sensor or "", {}).get("state", "off")
        bedtime = bool(rcfg.bedtime_entity) and states.get(rcfg.bedtime_entity or "", {}).get("state") == "on"
        unavailable = [s.key for s in snap.signals if not s.available]
        if unavailable:
            warnings.append(f"Signals unavailable: {', '.join(unavailable)}")
        st = self._compute_live_state(room_name, snap.occupied, media, zones)
        return RoomInputs(
            occupied=snap.occupied,
            ambient_lux=lux,
            media_state=media,
            bedtime=bedtime,
            zones=zones,
            entity_states=states,
            idle_minutes=snap.idle_minutes,
            signature=snap.signature,
            learned_state=(st or {}).get("intent"),
            state_name=(st or {}).get("name"),
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
        inputs = self.current_inputs(room_name)
        weights = self.overlay_weights.get(room_name)
        ev = resolve_room(
            rcfg,
            self.schemas,
            inputs,
            now,
            punches=self.punches.get(room_name),
            mode_entered_at=self.mode_entered_at.get(room_name),
            overlay_weights=weights,
            seasonal=self.seasonal_now(now),
            house=self.house_model(),
            pins=self.current_pins(room_name),
        )
        return {
            "evaluation": ev.model_dump(mode="json"),
            "inputs": inputs.model_dump(mode="json"),
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

        ev_after = self.evaluate_room(room_name, now=now)["evaluation"]
        role_after = ev_after["roles"].get(role)
        to_bri = role_after["brightness_pct"] if role_after else from_bri

        self.luxie.log_causal_event(
            room=room_name,
            role=role,
            from_bri=from_bri,
            to_bri=to_bri,
            actor=actor,
            reason=f"Manual change {delta_pct:+.0f}% (decays over {rcfg.punch_half_life_minutes:.0f} min)",
            breakdown=role_after["breakdown"] if role_after else {},
            ts=now,
        )
        return {"before": from_bri, "after": to_bri}

    async def tick_once(self) -> None:
        now = datetime.now()
        self._accumulate_net(now)
        for rname in list(self.rooms):
            self.update_presence(rname, now)
            self.step_darkness(rname, now)
            self.step_overlay_weights(rname, self.current_inputs(rname), now)
            data = self.evaluate_room(rname, now=now)
            ev = data["evaluation"]
            inputs = data["inputs"]

            # Animated schemas run off time-in-mode: restart the clock when the overlay set / state changes
            key = (tuple(ev["active_overlays"]), inputs.get("learned_state"))
            if self.mode_key.get(rname) != key:
                self.mode_key[rname] = key
                self.mode_entered_at[rname] = now

            if self.live:
                self._track_activity(rname, inputs, ev, now)
            cmds: list[dict[str, Any]] = []
            for role, r_dict in ev["roles"].items():
                prev_bri = self.last_applied_bri.get((rname, role))
                new_bri = r_dict["brightness_pct"]
                # Log real moves only: on/off, or a change of 2 %+ (target_lux jitter would flood the audit log)
                if prev_bri is None or (prev_bri == 0) != (new_bri == 0) or abs(new_bri - prev_bri) >= 2:
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
                rev_obj.transition_s = self.soft_start_seconds(rname, role, prev_bri, new_bri, now)
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
                runtime.luxie.prune_minutes()
                runtime.luxie.apply_retention({"ha_history": runtime.settings["detail_days"]})
                if runtime.last_vacuum is None or (now - runtime.last_vacuum).total_seconds() >= 7 * 86400:
                    runtime.last_vacuum = now
                    runtime.luxie.vacuum()
            # Phase 1 grid + intent model: refresh hourly from whatever history we hold
            if runtime.last_datalab_rebuild is None or (now - runtime.last_datalab_rebuild).total_seconds() >= 3600:
                runtime.last_datalab_rebuild = now
                if runtime.luxie.history_span()["rows"]:
                    for rname in runtime.rooms:
                        runtime.datalab.rebuild(rname)
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


class YamlSaveRequest(BaseModel):
    content: str


class DeviceDecisionRequest(BaseModel):
    host: str
    status: str  # accepted | ignored | clear
    zone: str | None = None
    name: str | None = None
    active_kbps: float | None = None


class ImportRequest(BaseModel):
    days: int = 10


class StateNameRequest(BaseModel):
    name: str | None = None


class SeasonalTuneRequest(BaseModel):
    values: dict[str, Any] = {}
    reset: bool = False
    locks: dict[str, bool] = {}


class HousePatchRequest(BaseModel):
    patch: dict[str, Any] = {}
    locks: dict[str, bool] = {}
    reset_paths: list[str] = []


class HouseImportRequest(BaseModel):
    overrides: dict[str, Any] = {}
    locks: list[str] = []


class MergeRequest(BaseModel):
    ids: list[int]


class LinkRequest(BaseModel):
    role: str
    mode: str
    brightness: float | None = None
    kelvin: int | None = None
    locked: bool | None = None


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
            {"room": r.room, "friendly_name": r.friendly_name, "roles": list(r.bulbs), "zones": list(r.presence.zones)}
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
    return {"causal_logs": runtime.luxie.get_causal_logs(room_name, limit=limit)}


@app.get("/api/presence/{room_name}")
async def get_presence(room_name: str) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    snap = runtime.presence_snap.get(room_name) or runtime.update_presence(room_name, datetime.now())
    return {
        "snapshot": snap.to_dict(),
        "recording": runtime.live,
        "signal_health": runtime.luxie.signal_health(room_name),
        "opnsense": runtime.net.status(),
    }


@app.get("/api/rooms/{room_name}/contested")
async def get_contested(room_name: str, hours: float = 24.0) -> dict[str, Any]:
    assert runtime is not None
    return {"lights": runtime.luxie.contested_lights(room_name, hours=hours)}


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


@app.get("/api/datalab/{room_name}")
async def datalab_summary(room_name: str) -> dict[str, Any]:
    """Phase 1 · Data: coverage, light traceback, lux profile, sleep/wake times, learned patterns."""
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    out = runtime.datalab.summary(room_name)
    runtime.current_inputs(room_name)
    out["live_state"] = runtime.live_state(room_name)
    out["ha_connected"] = runtime.bridge.connected
    return out


@app.get("/api/datalab/{room_name}/series")
async def datalab_series(room_name: str, range: str = "today", end: str | None = None) -> dict[str, Any]:
    """Time series for the dashboard: today (5-min), week (15-min) or month (hourly)."""
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    if range not in RANGES:
        raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
    days, bucket = RANGES[range]
    end_dt = datetime.fromisoformat(end) if end else datetime.now(runtime.datalab.tz()).replace(tzinfo=None)
    start_dt = end_dt.replace(hour=0, minute=0) if days is None else end_dt - timedelta(days=days)
    slots = runtime.luxie.load_slots(room_name)
    d_add_since_active(slots)
    out = d_series(slots, start_dt.strftime("%Y-%m-%dT%H:%M"), end_dt.strftime("%Y-%m-%dT%H:%M"), bucket)
    out.update({"range": range, "start": start_dt.isoformat(timespec="minutes"), "end": end_dt.isoformat(timespec="minutes")})
    return out


@app.post("/api/datalab/{room_name}/states/{state_id}/name")
async def datalab_name_state(room_name: str, state_id: int, req: StateNameRequest) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    try:
        return runtime.datalab.name_state(room_name, state_id, req.name)
    except KeyError:
        raise HTTPException(404, f"Unknown state {state_id}")


@app.get("/api/house/year")
async def datalab_year(year: int | None = None) -> dict[str, Any]:
    """The yearly model: sun for every hour of the year here, and the seasonal compensation it implies."""
    assert runtime is not None
    model = runtime.seasonal_model()
    now = datetime.now(runtime.datalab.tz())
    out = model.year_view(year or now.year)
    out["now"] = model.now(now)
    out["locks"] = runtime.seasonal_locks()
    out["limits"] = SEASONAL_LIMITS
    out["location_source"] = "settings" if runtime.settings.get("latitude") not in (None, "") else (
        "home_assistant" if runtime.bridge.latitude is not None else None)
    return out


def _room_or_404(room_name: str) -> None:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")


@app.get("/api/luxie/{room_name}/states")
async def switchboard_states(room_name: str) -> dict[str, Any]:
    """Luxie switchboard: every discovered state with its links."""
    _room_or_404(room_name)
    out = runtime.datalab.states(room_name)
    out["roles"] = list(runtime.rooms[room_name].bulbs)
    runtime.current_inputs(room_name)
    out["live"] = runtime.live_state(room_name)
    return out


@app.get("/api/luxie/{room_name}/states/{state_id}")
async def switchboard_state(room_name: str, state_id: int) -> dict[str, Any]:
    _room_or_404(room_name)
    try:
        return runtime.datalab.state_dataset(room_name, state_id)
    except KeyError:
        raise HTTPException(404, f"Unknown state {state_id}")


@app.post("/api/luxie/{room_name}/states/merge")
async def switchboard_merge(room_name: str, req: MergeRequest) -> dict[str, Any]:
    _room_or_404(room_name)
    try:
        return runtime.datalab.merge_states(room_name, req.ids)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/luxie/{room_name}/states/{state_id}/unmerge")
async def switchboard_unmerge(room_name: str, state_id: int) -> dict[str, Any]:
    _room_or_404(room_name)
    try:
        return runtime.datalab.unmerge_state(room_name, state_id)
    except KeyError:
        raise HTTPException(404, f"Unknown state {state_id}")


@app.post("/api/luxie/{room_name}/states/{state_id}/link")
async def switchboard_link(room_name: str, state_id: int, req: LinkRequest) -> dict[str, Any]:
    _room_or_404(room_name)
    try:
        return runtime.datalab.link_state(room_name, state_id, req.role, req.mode, req.brightness, req.kelvin,
                                          locked=req.locked)
    except KeyError:
        raise HTTPException(404, f"Unknown state {state_id}")
    except PermissionError as exc:
        raise HTTPException(423, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _drop_path(d: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.get(p) if isinstance(d, dict) else None
        if d is None:
            return
    if isinstance(d, dict):
        d.pop(parts[-1], None)


@app.get("/api/house/model")
async def house_model_get() -> dict[str, Any]:
    """The whole house model (Standard v1): effective values, what the UI overrode, locks, and any errors."""
    assert runtime is not None
    model, errors = runtime.house()
    return {"version": STANDARD_VERSION, "model": model.model_dump() if model else None, "errors": errors,
            "overrides": runtime.house_overrides(), "locks": runtime.house_locks(),
            "rooms": {n: {"lights": list(c.bulbs), "zones": list(c.presence.zones)} for n, c in runtime.rooms.items()},
            "curve_points": curve_points(model.curve) if model else []}


@app.post("/api/house/model")
async def house_model_patch(req: HousePatchRequest) -> dict[str, Any]:
    """Change settings. The whole model is re-validated; nothing is saved unless all of it is valid.
    Locked paths (and everything under them) refuse edits and resets until unlocked."""
    assert runtime is not None
    locks = set(runtime.house_locks())
    prior = set(locks)
    for path, on in req.locks.items():
        (locks.add if on else locks.discard)(path)
    hit = locked_hits(paths_in(req.patch) + req.reset_paths, sorted(prior & locks))
    if hit:
        raise HTTPException(423, f"locked: {', '.join(hit)} (unlock first)")
    overrides = deep_merge(runtime.house_overrides(), req.patch)
    for path in req.reset_paths:
        _drop_path(overrides, path)
    model, errors = build_house(runtime.settings, runtime.rooms, overrides)
    if errors:
        raise HTTPException(400, {"errors": errors})
    runtime.luxie.save_model("_house", "house_overrides", json.dumps(overrides))
    runtime.luxie.save_model("_house", "house_locks", json.dumps(sorted(locks)))
    return await house_model_get()


@app.get("/api/house/model/export")
async def house_model_export() -> dict[str, Any]:
    """One file to back up, diff, or move to a new install."""
    assert runtime is not None
    model, errors = runtime.house()
    return {"luxador_house_model": STANDARD_VERSION, "exported_at": datetime.now().isoformat(timespec="seconds"),
            "effective": model.model_dump() if model else None, "errors": errors,
            "overrides": runtime.house_overrides(), "locks": runtime.house_locks()}


@app.post("/api/house/model/import")
async def house_model_import(req: HouseImportRequest) -> dict[str, Any]:
    assert runtime is not None
    model, errors = build_house(runtime.settings, runtime.rooms, req.overrides)
    if errors:
        raise HTTPException(400, {"errors": errors})
    runtime.luxie.save_model("_house", "house_overrides", json.dumps(req.overrides))
    runtime.luxie.save_model("_house", "house_locks", json.dumps(sorted(set(req.locks))))
    return await house_model_get()


@app.get("/api/house/curve/suggest/{room_name}")
async def curve_suggest(room_name: str) -> dict[str, Any]:
    """Darkness thresholds learned from when you actually switched lights on."""
    _room_or_404(room_name)
    return suggest_thresholds(runtime.luxie.load_slots(room_name))


@app.get("/api/rooms/{room_name}/grid")
async def room_grid(room_name: str) -> dict[str, Any]:
    """Preview: what every light does for each darkness level x presence situation, right now."""
    _room_or_404(room_name)
    house = runtime.house_model()
    if not house:
        raise HTTPException(400, "house model invalid; fix it first")
    cfg = runtime.rooms[room_name]
    base = runtime.current_inputs(room_name)
    now = datetime.now()
    zones = list(cfg.presence.zones)
    situations = [("away", False, {})] + [("room", True, {z: False for z in zones})] + \
                 [(f"zone:{z}", True, {zz: zz == z for zz in zones}) for z in zones]
    levels = [("bright", 0.0), ("dim", 0.5), ("dark", 1.0)]
    cells = {}
    for sname, occ, zmap in situations:
        for lname, d in levels:
            inp = base.model_copy(update={"occupied": occ, "zones": zmap, "darkness": d, "media_state": "off",
                                          "bedtime": False, "learned_state": None})
            ev = resolve_room(cfg, runtime.schemas, inp, now, house=house, overlay_weights={})
            cells[f"{sname}|{lname}"] = {r: e.brightness_pct for r, e in ev.roles.items()}
    owners = {r: house.owner(room_name, r)[0] for r in cfg.bulbs}
    return {"situations": [s[0] for s in situations], "levels": [l[0] for l in levels], "cells": cells,
            "lights": list(cfg.bulbs), "owners": owners,
            "zone_modes": {z: house.rooms[room_name].zones.get(z).mode if z in house.rooms[room_name].zones else "join"
                           for z in zones}}


@app.get("/api/house/seasonal")
async def seasonal_get() -> dict[str, Any]:
    assert runtime is not None
    return {"effective": SeasonalConfig.from_settings(runtime.seasonal_settings()).__dict__ | {"extra": None},
            "from_settings_yaml": runtime.settings.get("seasonal") or {},
            "overrides": runtime.seasonal_overrides(), "limits": SEASONAL_LIMITS,
            "locks": runtime.seasonal_locks()}


@app.post("/api/house/seasonal")
async def seasonal_tune(req: SeasonalTuneRequest) -> dict[str, Any]:
    """Tune the seasonal lift live. Saved in the database (overrides settings.yaml).

    Locked settings can't be changed (by you, a reset, or any future auto-tune) until unlocked: send
    {"locks": {"key": false}} first. A reset keeps locked values.
    """
    assert runtime is not None
    prior_locks = set(runtime.seasonal_locks())
    locks = set(prior_locks)
    for k, on in req.locks.items():
        if k not in SEASONAL_LIMITS and k not in ("enabled", "day_start", "day_end"):
            raise HTTPException(400, f"unknown seasonal setting {k!r}")
        (locks.add if on else locks.discard)(k)
    current = runtime.seasonal_overrides()
    if req.reset:
        effective = SeasonalConfig.from_settings(runtime.seasonal_settings()).__dict__
        merged: dict[str, Any] = {k: effective[k] for k in locks}  # locked values survive a reset
    else:
        # "set it and lock it" in one request is fine; only locks that already existed (and stay) block edits
        blocked = sorted(k for k in req.values if k in prior_locks and k in locks)
        if blocked:
            raise HTTPException(423, f"locked: {', '.join(blocked)} (unlock first)")
        try:
            merged = {**current, **clean_overrides(req.values)}
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
    runtime.luxie.save_model("_house", "seasonal_overrides", json.dumps(merged))
    runtime.luxie.save_model("_house", "seasonal_locks", json.dumps(sorted(locks)))
    out = await datalab_year()
    out["locks"] = sorted(locks)
    return out


@app.get("/api/datalab/{room_name}/together")
async def datalab_together(room_name: str) -> dict[str, Any]:
    """What goes together: per signal, what's usually true with it, its usual times, and how likely it is now."""
    _room_or_404(room_name)
    from .together import analyse

    slots = runtime.luxie.load_slots(room_name)
    net: dict[str, dict] = {}
    for row in runtime.luxie._conn.execute("SELECT minute, host, hostname, kbps FROM net_minutes").fetchall():
        e = net.setdefault(row["host"], {"label": row["hostname"] or row["host"], "minutes": {}})
        e["minutes"][row["minute"]] = row["kbps"]
    return analyse(slots, net or None, now=datetime.now(runtime.datalab.tz()).replace(tzinfo=None),
                   net_active_kbps=runtime.settings["net_active_kbps"])


@app.post("/api/datalab/import")
async def datalab_import(req: ImportRequest) -> dict[str, Any]:
    """Pull recorder history (+ long-term lux statistics) from HA, then rebuild every room's grid and model."""
    assert runtime is not None
    if not runtime.bridge.configured:
        raise HTTPException(400, "Home Assistant is not connected")
    if runtime.datalab.status.get("state") == "importing":
        return runtime.datalab.status
    asyncio.create_task(runtime.datalab.import_history(days=max(1, min(req.days, 365))))
    return {"state": "importing", "days": req.days}


@app.post("/api/datalab/{room_name}/rebuild")
async def datalab_rebuild(room_name: str) -> dict[str, Any]:
    assert runtime is not None
    if room_name not in runtime.rooms:
        raise HTTPException(404, f"Unknown room: {room_name}")
    return runtime.datalab.rebuild(room_name)


@app.get("/api/house/areas")
async def house_areas() -> dict[str, Any]:
    """Phase 0: Home Assistant's areas vs the rooms Luxador is set up for (read-only)."""
    assert runtime is not None
    if not runtime.bridge.connected:
        return {"available": False, "reason": "Home Assistant not connected", "rooms": sorted(runtime.rooms)}
    try:
        areas, devices, entities = await runtime.bridge.fetch_registries()
    except Exception as exc:
        return {"available": False, "reason": f"Could not read HA areas: {exc}", "rooms": sorted(runtime.rooms)}
    return {"available": True, **house_overview(areas, devices, entities, runtime.bridge.states, runtime.rooms)}


@app.get("/api/storage")
async def storage() -> dict[str, Any]:
    """How big the database is, what's in it, and how long each table is kept."""
    assert runtime is not None
    rep = runtime.luxie.storage_report()
    rep["retention_days"] = {**rep["retention_days"], "ha_history": runtime.settings["detail_days"],
                             "slots (5-min grid)": runtime.settings["detail_days"],
                             "presence_minutes / net_minutes": 30, "seasonal": "forever (tiny)",
                             "lts_hourly": "forever (tiny)"}
    return rep


@app.get("/api/net/hosts")
async def get_net_hosts() -> dict[str, Any]:
    assert runtime is not None
    hosts = sorted(runtime.net.hosts, key=lambda h: -h.kbps)
    return {"status": runtime.net.status(), "hosts": [h.to_dict() for h in hosts]}


def run() -> None:
    settings = _load_settings()
    uvicorn.run("luxador.main:app", host=settings["host"], port=settings["port"], reload=False)


if __name__ == "__main__":
    run()
