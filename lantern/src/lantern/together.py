"""What goes together: a light-weight correlation test over the 5-minute grid.

Every signal becomes yes/no per 5-minute slot (someone home, each zone, TV, each light, dark outside, and the
busiest network devices). For each signal we report:

* goes_with: other signals that are usually true at the same time, with P(B | A) and lift = P(B | A) / P(B)
  (lift 3 = three times more likely than at a random moment);
* usual: the hours where it's on at least half the time, per weekday / weekend;
* starts: the median time it switches on, per weekday / weekend;
* now: P(signal | this hour, this daytype), the simplest honest prediction.

No model to train, nothing hidden: counts and ratios you can check by hand.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from statistics import median
from typing import Any

MIN_SUPPORT = 12          # a signal must be on for at least an hour in total to be reported
GOES_WITH_MIN_P = 0.6     # B is true at least 60 % of the time A is
GOES_WITH_MIN_LIFT = 1.3  # ...and that's clearly more than B's normal rate
USUAL_MIN_SHARE = 0.5     # an hour is "usual" if the signal is on in at least half of that hour's slots


def signal_columns(slots: list[dict[str, Any]], net: dict[str, dict[str, float]] | None = None,
                   net_active_kbps: float = 20.0, max_devices: int = 8) -> dict[str, dict[str, Any]]:
    """{key: {"label", "kind", "on": [bool|None per slot]}}. None = unknown for that slot."""
    cols: dict[str, dict[str, Any]] = {}

    def col(key: str, label: str, kind: str, fn) -> None:
        vals = [fn(s) for s in slots]
        if sum(1 for v in vals if v) >= MIN_SUPPORT:
            cols[key] = {"label": label, "kind": kind, "on": vals}

    col("home", "someone home", "presence", lambda s: None if s.get("occupied") is None else s["occupied"] == 1)
    zones = sorted({z for s in slots for z in (s.get("zones") or [])})
    for z in zones:
        col(f"zone.{z}", f"at {z}", "zone", lambda s, z=z: z in (s.get("zones") or []))
    col("tv", "TV on", "media", lambda s: None if s.get("media") is None else s["media"] == 1)
    col("dark", "dark outside", "sun", lambda s: None if s.get("sun_elev") is None else s["sun_elev"] < 0)
    roles = sorted({r for s in slots for r in (s.get("lights") or {})})
    for r in roles:
        col(f"light.{r}", f"{r} on", "light",
            lambda s, r=r: None if (s.get("lights") or {}).get(r) is None else s["lights"][r] > 0)

    if net:
        # net: {host: {"label": name, "minutes": {"YYYY-MM-DDTHH:MM": kbps}}}; a slot is 'on' if any minute in it was busy
        busy = sorted(net.items(), key=lambda kv: -sum(1 for v in kv[1]["minutes"].values() if v >= net_active_kbps))
        for host, info in busy[:max_devices]:
            mins = info["minutes"]

            def dev(s, mins=mins):
                t = datetime.fromisoformat(s["slot"])
                return any(mins.get(t.replace(minute=t.minute + k).strftime("%Y-%m-%dT%H:%M"), 0) >= net_active_kbps
                           for k in range(5) if t.minute + k < 60)

            col(f"net.{host}", f"{info.get('label') or host} busy", "device", dev)
    return cols


def _hhmm(m: float) -> str:
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _windows(share_by_hour: list[float | None]) -> list[dict[str, Any]]:
    """Runs of consecutive hours at or above USUAL_MIN_SHARE (wrapping past midnight)."""
    hot = [h for h in range(24) if (share_by_hour[h] or 0) >= USUAL_MIN_SHARE]
    if not hot:
        return []
    runs, start = [], hot[0]
    for a, b in zip(hot, hot[1:] + [None]):
        if b != a + 1:
            runs.append([start, a])
            start = b
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == 23:
        runs = [[runs[-1][0], runs[0][1]]] + runs[1:-1]
    out = []
    for a, b in runs:
        hours = list(range(a, b + 1)) if a <= b else list(range(a, 24)) + list(range(0, b + 1))
        out.append({"from": f"{a:02d}:00", "to": f"{(b + 1) % 24:02d}:00",
                    "p": round(sum(share_by_hour[h] or 0 for h in hours) / len(hours), 2)})
    return out


def _start_clusters(minutes: list[int], min_n: int = 3) -> list[dict[str, Any]]:
    """Things can start more than once a day (07:00 and 17:30), so one median would invent 12:15.
    Group start times that fall in neighbouring hours and report each group's median."""
    by_hour = Counter(m // 60 for m in minutes)
    groups: list[list[int]] = []
    for h in sorted(by_hour):
        if groups and h - groups[-1][-1] <= 1:
            groups[-1].append(h)
        else:
            groups.append([h])
    out = []
    for g in groups:
        mins = [m for m in minutes if m // 60 in g]
        if len(mins) >= min_n:
            out.append({"median": _hhmm(median(mins)), "n": len(mins)})
    return out


def analyse(slots: list[dict[str, Any]], net: dict[str, dict[str, float]] | None = None,
            now: datetime | None = None, net_active_kbps: float = 20.0) -> dict[str, Any]:
    cols = signal_columns(slots, net, net_active_kbps)
    if not slots or not cols:
        return {"signals": [], "slots": len(slots)}
    now = now or datetime.now()
    now_dt = "weekend" if now.weekday() >= 5 else "weekday"
    keys = list(cols)
    out = []
    for a in keys:
        on_a = cols[a]["on"]
        idx_a = [i for i, v in enumerate(on_a) if v]
        known_a = sum(1 for v in on_a if v is not None)

        goes = []
        for b in keys:
            if b == a:
                continue
            on_b = cols[b]["on"]
            both_known = [i for i in idx_a if on_b[i] is not None]
            if len(both_known) < MIN_SUPPORT:
                continue
            p_b_given_a = sum(1 for i in both_known if on_b[i]) / len(both_known)
            kb = [v for v in on_b if v is not None]
            p_b = sum(kb) / len(kb) if kb else 0
            lift = p_b_given_a / p_b if p_b else 0
            if p_b_given_a >= GOES_WITH_MIN_P and lift >= GOES_WITH_MIN_LIFT:
                goes.append({"key": b, "label": cols[b]["label"], "p": round(p_b_given_a, 2), "lift": round(lift, 1)})
        goes.sort(key=lambda g: (-g["p"] * min(g["lift"], 5)))

        # usual times + typical start, per daytype
        usual, starts, by_hour = {}, {}, {}
        for dt in ("weekday", "weekend"):
            tot, hit = Counter(), Counter()
            for s, v in zip(slots, on_a):
                if v is None or s["daytype"] != dt:
                    continue
                h = s["minute_of_day"] // 60
                tot[h] += 1
                hit[h] += 1 if v else 0
            share = [round(hit[h] / tot[h], 2) if tot[h] else None for h in range(24)]
            by_hour[dt] = share
            usual[dt] = _windows(share)
            st_mins = [slots[i]["minute_of_day"] for i in range(1, len(slots))
                       if on_a[i] and on_a[i - 1] is False and slots[i]["daytype"] == dt]
            starts[dt] = _start_clusters(st_mins)

        out.append({
            "key": a,
            "label": cols[a]["label"],
            "kind": cols[a]["kind"],
            "on_slots": len(idx_a),
            "share": round(len(idx_a) / known_a, 2) if known_a else None,
            "goes_with": goes[:6],
            "usual": usual,
            "starts": starts,
            "by_hour": by_hour,
            "now_p": by_hour[now_dt][now.hour],
        })
    out.sort(key=lambda x: ({"media": 0, "zone": 1, "device": 2, "presence": 3, "light": 4, "sun": 5}[x["kind"]], -x["on_slots"]))
    return {"signals": out, "slots": len(slots), "now": {"hour": now.hour, "daytype": now_dt},
            "rules": {"min_support_slots": MIN_SUPPORT, "goes_with_min_p": GOES_WITH_MIN_P,
                      "goes_with_min_lift": GOES_WITH_MIN_LIFT, "usual_min_share": USUAL_MIN_SHARE}}
