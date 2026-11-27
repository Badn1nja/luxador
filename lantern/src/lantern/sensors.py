"""Sensor profiles: turn a raw reading into a clean, comparable one, and say where every entity is attached.

Kept pure (no Home Assistant, no database) so it is easy to test and to trust. A profile is a preset plus
optional overrides. For a lux sensor the cleaned reading is:

    raw -> reject if outside [min_valid, max_valid] -> (raw - floor) * scale -> time-based smoothing
        -> relative brightness 0..100 (log scale between dark_lux and bright_lux)

Relative brightness is the sensor-independent number Lantern's curves can be built on: 0 = fully dark for this
sensor, 100 = as bright as this sensor is expected to get.
"""

from __future__ import annotations

import math
from typing import Any

FIELDS = {
    "floor_lux": (0.0, 5000.0),       # the sensor's own offset / light from our lamps that it always sees
    "scale": (0.05, 20.0),            # calibration multiplier
    "smooth_seconds": (0.0, 3600.0),  # time constant of the smoothing; 0 = off
    "min_valid": (0.0, 1e6),          # readings outside [min_valid, max_valid] are rejected as faulty
    "max_valid": (1.0, 1e6),
    "dark_lux": (0.05, 5000.0),       # reads as 0% relative brightness at or below this
    "bright_lux": (0.1, 1e6),         # reads as 100% at or above this
}

PRESETS: dict[str, dict[str, Any]] = {
    "raw": {"label": "Raw (no processing)", "floor_lux": 0.0, "scale": 1.0, "smooth_seconds": 0.0,
            "min_valid": 0.0, "max_valid": 1e6, "dark_lux": 1.0, "bright_lux": 10000.0},
    "indoor_room": {"label": "Indoor room (smoothed, room-scale)", "floor_lux": 0.0, "scale": 1.0, "smooth_seconds": 300.0,
                    "min_valid": 0.0, "max_valid": 20000.0, "dark_lux": 15.0, "bright_lux": 250.0},
    "window_sill": {"label": "Near a window (wide range)", "floor_lux": 0.0, "scale": 1.0, "smooth_seconds": 180.0,
                    "min_valid": 0.0, "max_valid": 100000.0, "dark_lux": 20.0, "bright_lux": 3000.0},
    "outdoor": {"label": "Outdoor (full daylight range)", "floor_lux": 0.0, "scale": 1.0, "smooth_seconds": 120.0,
                "min_valid": 0.0, "max_valid": 200000.0, "dark_lux": 1.0, "bright_lux": 30000.0},
}


class ProfileError(ValueError):
    pass


def make_profile(preset: str = "indoor_room", **overrides: Any) -> dict[str, Any]:
    """A validated profile: the preset's values with any overrides applied (unknown or out-of-range = error)."""
    if preset not in PRESETS:
        raise ProfileError(f"unknown preset {preset!r}; choose one of {', '.join(PRESETS)}")
    prof = {k: v for k, v in PRESETS[preset].items() if k != "label"}
    for k, v in overrides.items():
        if v is None:
            continue
        if k not in FIELDS:
            raise ProfileError(f"unknown setting {k!r}")
        lo, hi = FIELDS[k]
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ProfileError(f"{k} must be a number") from None
        if not lo <= v <= hi:
            raise ProfileError(f"{k} must be between {lo:g} and {hi:g}")
        prof[k] = v
    if prof["max_valid"] <= prof["min_valid"]:
        raise ProfileError("max_valid must be greater than min_valid")
    if prof["bright_lux"] <= prof["dark_lux"]:
        raise ProfileError("bright_lux must be greater than dark_lux")
    return {"preset": preset, **prof}


def relative_brightness(lux: float, profile: dict[str, Any]) -> float:
    """0..100 on a log scale between the profile's dark and bright points."""
    lo, hi = math.log(profile["dark_lux"]), math.log(profile["bright_lux"])
    v = (math.log(max(lux, 1e-3)) - lo) / (hi - lo)
    return round(100.0 * max(0.0, min(1.0, v)), 1)


def clean(raw: Any, profile: dict[str, Any], prev: float | None = None, dt_seconds: float = 0.0) -> dict[str, Any]:
    """One reading through the profile. `prev` is the previous smoothed lux, `dt_seconds` the time since it."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return {"valid": False, "why": "no number (unavailable or unknown)", "raw": raw, "lux": prev, "relative": None}
    if math.isnan(value) or not profile["min_valid"] <= value <= profile["max_valid"]:
        return {"valid": False, "why": f"{value:g} lx is outside the valid range {profile['min_valid']:g}-{profile['max_valid']:g}",
                "raw": value, "lux": prev, "relative": None if prev is None else relative_brightness(prev, profile)}
    adj = max(0.0, (value - profile["floor_lux"]) * profile["scale"])
    tau = profile["smooth_seconds"]
    if prev is None or tau <= 0 or dt_seconds <= 0:
        lux = adj
    else:
        a = 1.0 - math.exp(-dt_seconds / tau)
        lux = prev + a * (adj - prev)
    return {"valid": True, "raw": value, "lux": round(lux, 2), "relative": relative_brightness(lux, profile)}


def combine_media(states: dict[str, dict[str, Any]], entities: list[str]) -> str:
    """One state for several media devices: playing if any is, else the first real state, else off."""
    seen = [str((states.get(e) or {}).get("state", "unavailable")) for e in entities]
    for want in ("playing", "buffering"):
        if want in seen:
            return want
    real = [s for s in seen if s not in ("unavailable", "unknown")]
    return real[0] if real else "off"


def attachments(rooms: dict[str, dict[str, Any]], home_entities: list[str]) -> dict[str, list[str]]:
    """Where each entity is attached: 'home', 'room', or 'room › zone'. An entity can be in more than one place."""
    out: dict[str, list[str]] = {}

    def add(entity: str | None, where: str) -> None:
        if entity and where not in out.setdefault(entity, []):
            out[entity].append(where)

    for e in home_entities:
        add(e, "home")
    for rid, raw in rooms.items():
        label = raw.get("friendly_name") or rid
        pres = raw.get("presence") or {}
        zones = pres.get("zones") or {}
        light_zone = {g: z for z, zr in zones.items() for g in (zr.get("lights") or [])}
        for g, ents in (raw.get("bulbs") or {}).items():
            where = f"{label} › {light_zone[g]}" if g in light_zone else label
            for e in ents:
                add(e, where)
        for e in pres.get("sensors") or []:
            add(e, label)
        add(raw.get("lux_sensor"), label)
        for e in raw.get("media_sensors") or []:
            add(e, label)
        for z, zr in zones.items():
            for e in zr.get("presence") or []:
                add(e, f"{label} › {z}")
            add(zr.get("lux_sensor"), f"{label} › {z}")
    return out
