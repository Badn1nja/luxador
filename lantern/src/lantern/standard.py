"""The Lantern Standard v1 in code: the house model (HOUSE > ROOM > ZONE > LIGHT), its validation, and the
general curve. See docs/STANDARD.md; defaults and ranges here must match that document.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import RoomConfig

STANDARD_VERSION = 1
ROOM_ZONE = "room"  # the implicit zone every unassigned light belongs to; always joins
SOFT_START_SECONDS = {"fast": 2, "normal": 20, "slow": 120, "sunrise": 900}
DEFAULT_TRANSITION_S = 3


def _hhmm_ok(v: str) -> str:
    h, m = v.split(":")
    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise ValueError("must be HH:MM")
    return f"{int(h):02d}:{int(m):02d}"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CurveSettings(Strict):
    dark_lux: float = Field(15.0, ge=0.1, le=2000)
    bright_lux: float = Field(250.0, ge=0.2, le=100000)
    min_pct: float = Field(10.0, ge=0, le=100)
    max_pct: float = Field(100.0, ge=0, le=100)
    shape: float = Field(0.8, ge=0.3, le=3.0)
    night_from: str = "23:00"
    night_to: str = "06:00"
    night_max_pct: float = Field(35.0, ge=0, le=100)
    colour_schema: str = "sun"
    darkness_source: Literal["ambient", "sun"] = "ambient"

    @model_validator(mode="after")
    def _check(self) -> "CurveSettings":
        if self.bright_lux <= self.dark_lux:
            raise ValueError("bright_lux must be greater than dark_lux")
        if self.max_pct < self.min_pct:
            raise ValueError("max_pct must be at least min_pct")
        _hhmm_ok(self.night_from)
        _hhmm_ok(self.night_to)
        return self


class LightSettings(Strict):
    enabled: bool = True
    curve: Literal["natural", "brightness_only", "fixed", "switch"] = "natural"
    fixed_pct: float = Field(80.0, ge=1, le=100)
    scale: float = Field(1.0, ge=0.25, le=2.0)
    offset_pct: float = Field(0.0, ge=-50, le=50)
    min_pct: float = Field(0.0, ge=0, le=100)
    max_pct: float = Field(100.0, ge=0, le=100)
    kelvin_offset: int = Field(0, ge=-1000, le=1000)
    soft_start: Literal["never", "first_of_day", "always"] = "first_of_day"
    lux_gain: float | None = Field(None, ge=0, le=5000)  # lux this light adds at the room sensor at 100 % (learned or set)
    soft_start_speed: Literal["fast", "normal", "slow", "sunrise"] = "normal"

    @model_validator(mode="after")
    def _check(self) -> "LightSettings":
        if self.max_pct < self.min_pct:
            raise ValueError("max_pct must be at least min_pct")
        return self


class DaylightModel(Strict):
    """Indoor daylight ≈ A × sin(sun)^B (learned by Flint from your history)."""
    A: float = Field(ge=0, le=200000)
    B: float = Field(1.0, ge=0.1, le=5.0)


class ZoneSettings(Strict):
    mode: Literal["join", "own"] = "join"
    idle_pct: float = Field(0.0, ge=0, le=100)
    lights: list[str] = Field(default_factory=list)


class RoomModel(Strict):
    engine: Literal["standard", "rules"] = "standard"
    zones: dict[str, ZoneSettings] = Field(default_factory=dict)
    lights: dict[str, LightSettings] = Field(default_factory=dict)
    lux_base: float | None = Field(None, ge=0, le=5000)  # what the room's sensor reads in the dark, lamps off
    daylight: DaylightModel | None = None  # used when the lux sensor is unavailable


class HouseModel(Strict):
    version: int = STANDARD_VERSION
    curve: CurveSettings = Field(default_factory=CurveSettings)
    rooms: dict[str, RoomModel] = Field(default_factory=dict)

    def owner(self, room: str, light: str) -> tuple[str, ZoneSettings]:
        rm = self.rooms[room]
        for zname, z in rm.zones.items():
            if light in z.lights:
                return zname, z
        return ROOM_ZONE, ZoneSettings(mode="join")


# --------------------------------------------------------------------------- building & validating

def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def from_config(settings: dict[str, Any], rooms: dict[str, RoomConfig]) -> dict[str, Any]:
    """Layer 2 of the precedence: what settings.yaml and rooms/*.yaml say."""
    out: dict[str, Any] = {"version": STANDARD_VERSION, "curve": dict(settings.get("curve") or {}), "rooms": {}}
    for name, cfg in rooms.items():
        raw = cfg.standard or {}
        zones: dict[str, Any] = {}
        for zname, z in cfg.presence.zones.items():
            zones[zname] = {"lights": list(z.lights), **({"mode": z.mode} if z.mode else {}),
                            **({"idle_pct": z.idle_pct} if z.idle_pct is not None else {})}
        out["rooms"][name] = deep_merge({"engine": raw.get("engine", "standard"), "zones": zones,
                                         "lights": {role: {} for role in cfg.bulbs}},
                                        {"lights": raw.get("lights") or {}})
    return out


def _flatten_errors(exc: ValidationError) -> list[str]:
    msgs = []
    for e in exc.errors():
        path = ".".join(str(p) for p in e["loc"])
        msgs.append(f"{path}: {e['msg'].replace('Value error, ', '')}" if path else e["msg"].replace("Value error, ", ""))
    return msgs


def build_house(settings: dict[str, Any], rooms: dict[str, RoomConfig],
                overrides: dict[str, Any] | None = None) -> tuple[HouseModel | None, list[str]]:
    """defaults < the house structure < UI settings, validated as one piece. Returns (model, errors).

    Zone ownership (which zone owns which light, join/own, idle) is part of the structure, so any `zones` in the
    overrides are ignored. Settings for a room or light group that doesn't exist (removed, renamed, merged) are
    left out of the model but kept in storage, so re-adding it picks them up again.
    """
    ov = copy.deepcopy(overrides or {})
    for rname in list((ov.get("rooms") or {})):
        if rname not in rooms:
            ov["rooms"].pop(rname)
            continue
        r = ov["rooms"][rname]
        r.pop("zones", None)
        for light in list((r.get("lights") or {})):
            if light not in rooms[rname].bulbs:
                r["lights"].pop(light)
    merged = deep_merge(from_config(settings, rooms), ov)
    try:
        model = HouseModel.model_validate(merged)
    except ValidationError as exc:
        return None, _flatten_errors(exc)
    errors = []
    for rname, rm in model.rooms.items():
        if rname not in rooms:
            errors.append(f"rooms.{rname}: no such room")
            continue
        known = set(rooms[rname].bulbs)
        seen: dict[str, str] = {}
        for zname, z in rm.zones.items():
            if zname == ROOM_ZONE:
                errors.append(f"rooms.{rname}.zones.{ROOM_ZONE}: reserved name")
            for light in z.lights:
                if light not in known:
                    errors.append(f"rooms.{rname}.zones.{zname}.lights: unknown light {light!r}")
                elif light in seen:
                    errors.append(f"rooms.{rname}.zones.{zname}.lights: {light!r} is already owned by zone {seen[light]!r}")
                seen[light] = zname
        for light in rm.lights:
            if light not in known:
                errors.append(f"rooms.{rname}.lights.{light}: unknown light")
    return (model if not errors else None), errors


def paths_in(patch: dict[str, Any], prefix: str = "") -> list[str]:
    """Dotted paths of every leaf a patch touches (for locks)."""
    out = []
    for k, v in patch.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and v:
            out += paths_in(v, p)
        else:
            out.append(p)
    return out


def locked_hits(patch_paths: list[str], locks: list[str]) -> list[str]:
    """A lock on a.b protects a.b and everything under it."""
    return sorted({p for p in patch_paths for lk in locks if p == lk or p.startswith(lk + ".")})


# --------------------------------------------------------------------------- the general curve

def _hm(v: str) -> int:
    h, m = v.split(":")
    return int(h) * 60 + int(m)


def in_window(minute: int, start: str, end: str) -> bool:
    a, b = _hm(start), _hm(end)
    return a <= minute < b if a <= b else (minute >= a or minute < b)


def darkness_from_lux(lux: float, curve: CurveSettings) -> float:
    lux = max(0.01, lux)
    hi, lo = math.log(curve.bright_lux), math.log(curve.dark_lux)
    return max(0.0, min(1.0, (hi - math.log(lux)) / (hi - lo)))


def darkness_from_sun(elev: float | None) -> float | None:
    if elev is None:
        return None
    return max(0.0, min(1.0, (10.0 - elev) / 16.0))


def curve_level(d: float, curve: CurveSettings) -> float:
    if d <= 0:
        return 0.0
    return curve.min_pct + (curve.max_pct - curve.min_pct) * (d ** curve.shape)


def curve_points(curve: CurveSettings, n: int = 60) -> list[dict[str, float]]:
    """Brightness vs indoor daylight, for drawing the curve (log-spaced lux)."""
    lo, hi = math.log10(max(0.1, curve.dark_lux / 4)), math.log10(curve.bright_lux * 2)
    pts = []
    for i in range(n):
        lux = 10 ** (lo + (hi - lo) * i / (n - 1))
        pts.append({"lux": round(lux, 2), "pct": round(curve_level(darkness_from_lux(lux, curve), curve), 1)})
    return pts


# --------------------------------------------------------------------------- learning the thresholds

def suggest_thresholds(slots: list[dict[str, Any]]) -> dict[str, Any]:
    """From history: the indoor lux just before you switched lights on (while someone was home), versus the lux
    while you were home and left them off. Lights add their own lux, so only 'before switch-on' slots count."""
    before_on, left_off = [], []
    for prev, cur in zip(slots, slots[1:]):
        if cur.get("occupied") != 1 or prev.get("lux_in") is None:
            continue
        if (prev.get("light_max") or 0) == 0 and (cur.get("light_max") or 0) > 0:
            before_on.append(prev["lux_in"])
        elif (prev.get("light_max") or 0) == 0 and (cur.get("light_max") or 0) == 0:
            left_off.append(prev["lux_in"])
    if len(before_on) < 8:
        return {"enough_data": False, "switch_ons": len(before_on)}

    def q(v: list[float], p: float) -> float:
        s = sorted(v)
        return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]

    if q(before_on, 0.9) < 3:
        return {"enough_data": False, "switch_ons": len(before_on),
                "reason": "every switch-on happened in near-darkness (under 3 lux), so the lux sensor can't tell us "
                          "where 'needs light' starts. Keeping your current thresholds."}
    bright = max(q(before_on, 0.9), q(left_off, 0.25) if left_off else 0)
    dark = max(0.5, min(q(before_on, 0.25), bright / 2))
    return {"enough_data": True, "switch_ons": len(before_on), "dark_lux": round(dark, 1),
            "bright_lux": round(max(bright, dark * 2), 1),
            "note": f"From {len(before_on)} times you switched a light on while home: you rarely do above "
                    f"~{round(bright)} lux, and almost always below ~{round(dark)} lux."}
