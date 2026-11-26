"""Flint's lux model: what each lamp adds at the room's (indoor) lux sensor, and how much daylight the room
gets for a given sun height. Learned from Echo's 5-minute grid with plain least squares; no black box.

1. Lamp gains (night only, sun below -6°, so there's no daylight to confuse things):
       lux_in ≈ base + Σ gain_r × level_r/100
   `base` is what the sensor reads in the dark with every lamp off (its floor / offset).
2. Daylight (sun up): daylight = lux_in − base − Σ gain_r × level_r/100, then
       daylight ≈ A × sin(sun)^B          (fitted on logs)
   plus the spread between cloudy (p25) and bright (p75) days as multipliers of that curve.

Nothing here changes how lights behave until the learned numbers are applied to the house model.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any

NIGHT_SUN = -6.0   # civil twilight: below this there's no daylight worth counting
DAY_SUN = 2.0      # above this, daylight dominates
MIN_ROWS = 24      # two hours of 5-minute slots


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a small square system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def _lstsq(rows: list[list[float]], y: list[float], ridge: float = 1e-3) -> list[float] | None:
    k = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) + (ridge if i == j and i > 0 else 0.0) for j in range(k)] for i in range(k)]
    aty = [sum(r[i] * yy for r, yy in zip(rows, y)) for i in range(k)]
    return _solve(ata, aty)


def _r2(y: list[float], pred: list[float]) -> float:
    mean = sum(y) / len(y)
    ss_tot = sum((v - mean) ** 2 for v in y) or 1e-9
    return round(1 - sum((v - p) ** 2 for v, p in zip(y, pred)) / ss_tot, 3)


def reporting_roles(slots: list[dict[str, Any]], roles: list[str]) -> tuple[list[str], list[str]]:
    """Lamps whose level is known in at least half the lux readings; the rest (e.g. long unavailable) are left out
    of the fits instead of blocking them."""
    with_lux = [s for s in slots if s.get("lux_in") is not None]
    if not with_lux:
        return [], list(roles)
    ok = [r for r in roles if sum(1 for s in with_lux if (s.get("lights") or {}).get(r) is not None) >= 0.5 * len(with_lux)]
    return ok, [r for r in roles if r not in ok]


def fit_lamp_gains(slots: list[dict[str, Any]], all_roles: list[str]) -> dict[str, Any]:
    """Non-negative least squares (drop any lamp that comes out negative and refit)."""
    roles, no_data = reporting_roles(slots, all_roles)
    data = [s for s in slots
            if s.get("lux_in") is not None and s.get("sun_elev") is not None and s["sun_elev"] < NIGHT_SUN
            and all((s.get("lights") or {}).get(r) is not None for r in roles)]
    if len(data) < MIN_ROWS:
        return {"ok": False, "reason": f"only {len(data)} night slots with lux + light data (need {MIN_ROWS})", "n": len(data),
                "no_data": no_data}
    active = [r for r in roles if sum(1 for s in data if s["lights"][r] > 0) >= 6]  # a lamp must be seen on at night
    unseen = [r for r in roles if r not in active]

    # Lamps that are (almost) always at the same level can't be told apart: fit each such group as one column.
    groups: list[list[str]] = []
    for r in active:
        for g in groups:
            same = sum(1 for s in data if s["lights"][r] == s["lights"][g[0]]) / len(data)
            if same >= 0.98:
                g.append(r)
                break
        else:
            groups.append([r])
    cols = ["+".join(g) for g in groups]
    level = {"+".join(g): (lambda s, g=g: s["lights"][g[0]]) for g in groups}
    active = cols
    while True:
        rows = [[1.0] + [level[c](s) / 100.0 for c in active] for s in data]
        y = [float(s["lux_in"]) for s in data]
        coef = _lstsq(rows, y)
        if coef is None:
            return {"ok": False, "reason": "lamps always switch together, so their shares can't be told apart", "n": len(data)}
        neg = [r for r, g in zip(active, coef[1:]) if g < 0]
        if not neg:
            break
        active = [r for r in active if r not in neg]
        unseen += neg
    pred = [sum(c * x for c, x in zip(coef, row)) for row in rows]
    gains: dict[str, float | None] = {}
    together: list[dict[str, Any]] = []
    for col, g in zip(active, coef[1:]):
        members = col.split("+")
        if len(members) == 1:
            gains[col] = round(g, 1)
        else:  # report the group; individual shares are unknowable from this data
            together.append({"lights": members, "combined_gain": round(g, 1)})
            for m in members:
                gains[m] = None
    for c in unseen:
        for r in c.split("+"):
            gains[r] = None
    for r in no_data:
        gains[r] = None
    not_learned = [r for r in roles if gains.get(r) is None and not any(r in t["lights"] for t in together)]
    return {"ok": True, "n": len(data), "base_lux": round(max(0.0, coef[0]), 2), "gains": gains, "r2": _r2(y, pred),
            "together": together, "not_learned": not_learned, "no_data": no_data}


def daylight_points(slots: list[dict[str, Any]], all_roles: list[str], base: float, gains: dict[str, float | None]) -> list[tuple[float, float]]:
    roles, _ = reporting_roles(slots, all_roles)
    pts = []
    for s in slots:
        e, lux = s.get("sun_elev"), s.get("lux_in")
        if e is None or lux is None or e < DAY_SUN:
            continue
        lamps = 0.0
        for r in roles:
            lvl = (s.get("lights") or {}).get(r)
            if lvl is None:
                break
            lamps += (gains.get(r) or 0.0) * lvl / 100.0
        else:
            d = lux - base - lamps
            if d > 0.5:
                pts.append((round(e, 2), round(d, 1)))
    return pts


def fit_daylight(points: list[tuple[float, float]]) -> dict[str, Any]:
    """log(daylight) = log(A) + B·log(sin(sun)). Returns A, B, fit quality and the cloudy/bright spread."""
    if len(points) < MIN_ROWS:
        return {"ok": False, "reason": f"only {len(points)} daytime slots with a lux reading (need {MIN_ROWS})", "n": len(points)}
    xs = [math.log(math.sin(math.radians(e))) for e, _ in points]
    ys = [math.log(d) for _, d in points]
    coef = _lstsq([[1.0, x] for x in xs], ys, ridge=0.0)
    if coef is None:
        return {"ok": False, "reason": "the sun barely moved in the data", "n": len(points)}
    a, b = math.exp(coef[0]), coef[1]
    pred = [coef[0] + b * x for x in xs]
    ratios = sorted(math.exp(y - p) for y, p in zip(ys, pred))
    q = lambda p: ratios[min(len(ratios) - 1, int(p * (len(ratios) - 1)))]  # noqa: E731
    return {"ok": True, "n": len(points), "A": round(a, 1), "B": round(b, 2), "r2_log": _r2(ys, pred),
            "cloudy": round(q(0.25), 2), "bright": round(q(0.75), 2)}


def predict_daylight(model: dict[str, Any] | None, sun_elev: float | None) -> float | None:
    if not model or not model.get("A") or sun_elev is None:
        return None
    if sun_elev <= 0:
        return 0.0
    return model["A"] * math.sin(math.radians(sun_elev)) ** model.get("B", 1.0)


def sensor_health(slots: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [s.get("lux_in") for s in slots]
    known = [v for v in vals if v is not None]
    longest, run, prev = 0, 0, object()
    for v in known:
        run = run + 1 if v == prev else 1
        longest, prev = max(longest, run), v
    return {
        "missing_share": round(1 - len(known) / len(vals), 3) if vals else None,
        "longest_flat_hours": round(longest * 5 / 60, 1),
        "min": round(min(known), 1) if known else None,
        "median": round(median(known), 1) if known else None,
        "max": round(max(known), 1) if known else None,
    }


def learn(slots: list[dict[str, Any]], roles: list[str]) -> dict[str, Any]:
    lamps = fit_lamp_gains(slots, roles)
    base = lamps.get("base_lux", 0.0) if lamps.get("ok") else 0.0
    gains = lamps.get("gains", {}) if lamps.get("ok") else {}
    pts = daylight_points(slots, roles, base, gains)
    day = fit_daylight(pts)
    curve = []
    if day.get("ok"):
        for e in range(2, 62, 3):
            mid = predict_daylight(day, e)
            curve.append({"sun": e, "lux": round(mid, 1), "cloudy": round(mid * day["cloudy"], 1), "bright": round(mid * day["bright"], 1)})
    step = max(1, -(-len(pts) // 400))  # ceiling division: at most 400 points for the chart
    return {"sensor": sensor_health(slots), "lamps": lamps, "daylight": day,
            "scatter": [{"sun": e, "lux": d} for e, d in pts[::step]], "curve": curve}
