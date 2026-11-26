"""The yearly model: where the sun is for every hour of the year at this house, and seasonal compensation.

The sun is computed, not learned (NOAA-style approximation, ~0.5° accuracy), from the latitude/longitude
Home Assistant already knows, so it works from day one and in either hemisphere.

Compensation (opt-in, `seasonal.enabled`):
  * Winter lift: the shorter the day versus the equinox, the more daytime brightness (up to
    `winter_boost_max_pct`) and slightly cooler light (`winter_kelvin_shift`) inside the day window.
  * Summer later: when sunset (clock time, so DST counts) is later than at the equinox, the evening curve
    shifts later by `summer_shift_factor` of the difference, capped at `summer_later_max_min`.
  * Winter earlier: the mirror image, default 0 (pulling evenings earlier in winter makes them gloomier).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any

HORIZON = -0.833  # sunrise/sunset: refraction + solar disc


def sun_elevation(lat: float, lon: float, when: datetime) -> float:
    """Solar elevation in degrees at a UTC-aware time."""
    jd = when.astimezone(timezone.utc).timestamp() / 86400.0 + 2440587.5
    n = jd - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    ha = math.radians((gmst * 15 + lon) % 360) - ra
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)))


def _crossings(lat: float, lon: float, day: date, tz: tzinfo) -> tuple[datetime | None, datetime | None, float]:
    """Sunrise, sunset (local, aware) and noon elevation by scanning the local day at 5-min steps."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    prev_t, prev_e = start, sun_elevation(lat, lon, start)
    rise = sett = None
    noon = prev_e
    for k in range(1, 289):
        t = start + timedelta(minutes=5 * k)
        e = sun_elevation(lat, lon, t)
        noon = max(noon, e)
        if prev_e < HORIZON <= e and rise is None:
            rise = prev_t + (t - prev_t) * ((HORIZON - prev_e) / (e - prev_e))
        if prev_e >= HORIZON > e:
            sett = prev_t + (t - prev_t) * ((prev_e - HORIZON) / (prev_e - e))
        prev_t, prev_e = t, e
    return rise, sett, noon


@dataclass
class SeasonalConfig:
    enabled: bool = False
    winter_boost_max_pct: float = 15.0
    winter_kelvin_shift: int = 300
    summer_shift_factor: float = 0.33
    summer_later_max_min: float = 45.0
    winter_earlier_max_min: float = 0.0
    day_start: str = "07:00"   # winter lift applies from here...
    day_end: str = "20:00"     # ...until here
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, raw: dict[str, Any] | None) -> "SeasonalConfig":
        raw = dict(raw or {})
        known = {k: raw.pop(k) for k in list(raw) if k in cls.__dataclass_fields__ and k != "extra"}
        return cls(**known, extra=raw)


# What can be tuned live, with safe bounds (the UI sliders use the same ranges)
LIMITS: dict[str, tuple[float, float]] = {
    "winter_boost_max_pct": (0, 40),
    "winter_kelvin_shift": (0, 1500),
    "summer_shift_factor": (0, 1),
    "summer_later_max_min": (0, 120),
    "winter_earlier_max_min": (0, 120),
}


def clean_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a live tuning request: known keys only, numbers clamped, HH:MM window."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k == "enabled":
            out[k] = bool(v)
        elif k in LIMITS:
            lo, hi = LIMITS[k]
            out[k] = max(lo, min(hi, float(v)))
            if k == "winter_kelvin_shift":
                out[k] = int(out[k])
        elif k in ("day_start", "day_end"):
            _hm(str(v))  # raises on bad input
            out[k] = str(v)
        else:
            raise ValueError(f"unknown seasonal setting {k!r}")
    if "day_start" in out and "day_end" in out and _hm(out["day_start"]) >= _hm(out["day_end"]):
        raise ValueError("day_start must be before day_end")
    return out


def _hm(v: str) -> int:
    h, m = v.split(":")
    return int(h) * 60 + int(m)


@lru_cache(maxsize=8)
def year_table(lat: float, lon: float, tz_name: str, year: int) -> dict[str, Any]:
    """Per day: sunrise/sunset/day length/noon elevation; per hour (x:30): elevation. 365(6) x 24."""
    from .datalab import get_tz

    tz = get_tz(tz_name)
    days = []
    hourly = []
    d = date(year, 1, 1)
    while d.year == year:
        rise, sett, noon = _crossings(lat, lon, d, tz)
        if rise and sett:
            day_h = (sett - rise).total_seconds() / 3600
        else:  # polar day / night
            day_h = 24.0 if noon > HORIZON else 0.0
        days.append({
            "date": d.isoformat(),
            "doy": d.timetuple().tm_yday,
            "sunrise": rise.strftime("%H:%M") if rise else None,
            "sunset": sett.strftime("%H:%M") if sett else None,
            "sunset_min": sett.hour * 60 + sett.minute if sett else None,
            "day_hours": round(day_h, 2),
            "noon_elev": round(noon, 1),
        })
        base = datetime(d.year, d.month, d.day, tzinfo=tz)
        hourly.append([round(sun_elevation(lat, lon, base + timedelta(hours=h, minutes=30)), 1) for h in range(24)])
        d += timedelta(days=1)
    # equinox reference: the day closest to 12 h of daylight in spring
    spring = [x for x in days if 60 <= x["doy"] <= 110 and x["sunset_min"] is not None]
    ref = min(spring, key=lambda x: abs(x["day_hours"] - 12.0)) if spring else days[79]
    min_day = min(x["day_hours"] for x in days)
    return {"days": days, "hourly_elev": hourly, "ref_day": ref["date"], "ref_sunset_min": ref["sunset_min"],
            "min_day_hours": min_day, "max_day_hours": max(x["day_hours"] for x in days)}


def compensation_for_day(day: dict[str, Any], table: dict[str, Any], cfg: SeasonalConfig) -> dict[str, Any]:
    span = 12.0 - table["min_day_hours"]
    deficit = max(0.0, min(1.0, (12.0 - day["day_hours"]) / span)) if span > 0.25 else 0.0
    shift = 0.0
    if day["sunset_min"] is not None and table["ref_sunset_min"] is not None:
        diff = (day["sunset_min"] - table["ref_sunset_min"]) * cfg.summer_shift_factor
        shift = min(cfg.summer_later_max_min, diff) if diff > 0 else max(-cfg.winter_earlier_max_min, diff)
    sunset_diff = (day["sunset_min"] - table["ref_sunset_min"]) if day["sunset_min"] is not None and table["ref_sunset_min"] is not None else None
    return {
        "math": {
            "day_hours": day["day_hours"], "equinox_hours": 12.0, "shortest_hours": round(table["min_day_hours"], 2),
            "deficit_formula": "(12 h − today) ÷ (12 h − shortest day)",
            "sunset_vs_equinox_min": sunset_diff, "summer_shift_factor": cfg.summer_shift_factor,
            "boost_formula": f"{cfg.winter_boost_max_pct:g}% × deficit",
            "shift_formula": f"sunset difference × {cfg.summer_shift_factor:g}, capped +{cfg.summer_later_max_min:g} / −{cfg.winter_earlier_max_min:g} min",
        },
        "deficit": round(deficit, 2),
        "boost_pct": round(cfg.winter_boost_max_pct * deficit, 1),
        "kelvin_shift": int(round(cfg.winter_kelvin_shift * deficit)),
        "evening_shift_min": round(shift),  # + = evening curve later
    }


class SeasonalModel:
    def __init__(self, lat: float | None, lon: float | None, tz_name: str, cfg: SeasonalConfig) -> None:
        self.lat, self.lon, self.tz_name, self.cfg = lat, lon, tz_name, cfg

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None

    def table(self, year: int) -> dict[str, Any]:
        return year_table(round(self.lat, 3), round(self.lon, 3), self.tz_name, year)

    def now(self, when: datetime) -> dict[str, Any]:
        """Compensation in force at a local time. Always computed; `applies` says whether it touches lights."""
        if not self.located:
            return {"located": False, "applies": False, "reason": "no latitude/longitude yet"}
        t = self.table(when.year)
        day = t["days"][when.timetuple().tm_yday - 1]
        comp = compensation_for_day(day, t, self.cfg)
        m = when.hour * 60 + when.minute
        in_day = _hm(self.cfg.day_start) <= m < _hm(self.cfg.day_end)
        return {
            "located": True,
            "enabled": self.cfg.enabled,
            "applies": self.cfg.enabled,
            "day": day,
            **comp,
            "boost_now_pct": comp["boost_pct"] if in_day else 0.0,
            "kelvin_now": comp["kelvin_shift"] if in_day else 0,
            # resolver warp: + pulls the clock forward (evening earlier), so a later evening is a negative warp
            "warp_minutes": -comp["evening_shift_min"],
        }

    def year_view(self, year: int) -> dict[str, Any]:
        if not self.located:
            return {"located": False}
        t = self.table(year)
        rows = [{**d, **compensation_for_day(d, t, self.cfg)} for d in t["days"]]
        return {"located": True, "year": year, "days": rows, "hourly_elev": t["hourly_elev"], "ref_day": t["ref_day"],
                "min_day_hours": t["min_day_hours"], "max_day_hours": t["max_day_hours"],
                "config": {k: v for k, v in self.cfg.__dict__.items() if k != "extra"}}
