"""Synthetic Home Assistant history for tests: a believable two-week routine for the Lounge.

Weekdays: wake 07:00, out 08:30-17:30, TV 20:00-22:30, bed 23:15.
Weekends: wake 09:30, home all day, TV 20:30-23:30, bed 00:30.
Every 3rd night: a 03:00 trip with the shoe light at 10 % for 10 minutes.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")
ENT = {
    "radar": "binary_sensor.living_room_ld2450_radar_1_radar_presence",
    "desk": "binary_sensor.living_room_ld2450_radar_1_zone_1",
    "sofa": "binary_sensor.living_room_ld2450_radar_1_zone_2",
    "tv": "media_player.living_room_tv_2",
    "lux": "sensor.living_room_lux_lounge_illuminance",
    "overhead": ["light.living_room_threelight_l", "light.living_room_threelight_m", "light.living_room_threelight_t"],
    "shoe": "light.shoelight_light",
    "loungetv": "light.loungetv",
    "sun": "sun.sun",
}


def _st(entity: str, t: datetime, state: str, **attrs) -> dict:
    return {"entity_id": entity, "state": state, "attributes": attrs,
            "last_changed": t.astimezone(timezone.utc).isoformat()}


def _light(entity: str, t: datetime, pct: float) -> dict:
    if pct <= 0:
        return _st(entity, t, "off")
    return _st(entity, t, "on", brightness=round(pct * 2.55), color_temp_kelvin=3000)


def household(days: int = 14, end_local: datetime | None = None) -> list[dict]:
    end_local = end_local or datetime(2026, 11, 26, 12, 0, tzinfo=TZ)
    start_day = (end_local - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    ev: list[dict] = []
    d = start_day
    n = 0
    while d < end_local:
        weekend = d.weekday() >= 5

        def at(h: float) -> datetime:
            return d + timedelta(hours=h)

        wake = 9.5 if weekend else 7.0
        bed = 24.5 if weekend else 23.25
        tv_on, tv_off = (20.5, 23.5) if weekend else (20.0, 22.5)
        # wake up: presence + overhead
        ev += [_st(ENT["radar"], at(wake), "on"), _st(ENT["sofa"], at(wake), "on")]
        ev += [_light(e, at(wake), 70) for e in ENT["overhead"]]
        if not weekend:
            ev += [_st(ENT["radar"], at(8.5), "off"), _st(ENT["sofa"], at(8.5), "off")]
            ev += [_light(e, at(8.5), 0) for e in ENT["overhead"]]
            ev += [_st(ENT["radar"], at(17.5), "on"), _st(ENT["desk"], at(17.5), "on")]
            ev += [_light(e, at(17.5), 60) for e in ENT["overhead"]]
            ev += [_st(ENT["desk"], at(19.9), "off"), _st(ENT["sofa"], at(19.9), "on")]
        ev += [_st(ENT["tv"], at(tv_on), "playing"), _light(ENT["loungetv"], at(tv_on), 60)]
        ev += [_light(e, at(tv_on), 30) for e in ENT["overhead"]]
        ev += [_st(ENT["tv"], at(tv_off), "off"), _light(ENT["loungetv"], at(tv_off), 0)]
        # bed: everything off
        ev += [_light(e, at(bed), 0) for e in ENT["overhead"]]
        ev += [_st(ENT["radar"], at(bed), "off"), _st(ENT["sofa"], at(bed), "off"), _light(ENT["shoe"], at(bed), 0)]
        if n % 3 == 0:  # 3am trip next night
            ev += [_light(ENT["shoe"], at(27.0), 10), _st(ENT["radar"], at(27.0), "on"),
                   _light(ENT["shoe"], at(27.0 + 10 / 60), 0), _st(ENT["radar"], at(27.0 + 10 / 60), "off")]
        # sun + daylight lux every 30 min
        for k in range(48):
            t = at(k / 2)
            elev = round(15 - 65 * abs((k / 2) - 12) / 12, 1)  # winter-ish: peak 15 deg at noon
            ev.append(_st(ENT["sun"], t, "above_horizon" if elev > 0 else "below_horizon", elevation=elev))
            ev.append(_st(ENT["lux"], t, str(round(max(1.0, 8 * elev), 1))))
        d += timedelta(days=1)
        n += 1
    return [e for e in ev if datetime.fromisoformat(e["last_changed"]) <= end_local]
