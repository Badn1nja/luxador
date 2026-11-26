"""Discovered states: let the data say what the house's states are (up to `max_states`, default 28).

1. Each 5-minute slot becomes a *context* vector: time of day (on a circle), weekday/weekend, sun, presence,
   each zone, TV, and time since last activity. Light levels are NOT in it: a state must never mean "the
   lights were on", or learning would just copy whatever the lights already did.
2. Identical contexts are merged (weights = number of slots), which turns ~9k slots/month into a few
   hundred points, so a weighted k-means in pure Python is fast enough for a Raspberry Pi.
3. k is chosen by silhouette score: the largest k within 10 % of the best score, so it prefers detail when
   the data supports it, never more than max_states, and every state must cover >= 1 % of the data.
4. Per state, each light role's real behaviour is summarised (on ratio, median brightness, median kelvin):
   the first, read-only draft of each bulb learning its own rule for that condition.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from statistics import median
from typing import Any

SUN_V = {"night": 0.0, "twilight": 0.33, "low": 0.66, "high": 1.0}
SINCE_V = {"now": 0.0, "<30m": 0.25, "<2h": 0.5, "<6h": 0.75, "6h+": 1.0}
NAME_MATCH_DISTANCE = 0.35


def _since_bucket(m: float | None) -> str | None:
    if m is None:
        return None
    if m <= 0:
        return "now"
    return "<30m" if m <= 30 else "<2h" if m <= 120 else "<6h" if m <= 360 else "6h+"


def _sun_bucket(e: float | None) -> str | None:
    if e is None:
        return None
    return "night" if e < -6 else "twilight" if e < 0 else "low" if e < 15 else "high"


def context_key(s: dict[str, Any], zones: list[str]) -> tuple:
    """Discrete context of a slot (time rounded to 30 min). Hashable, so identical contexts merge."""
    return (
        (s["minute_of_day"] // 30) * 30,
        s["daytype"],
        _sun_bucket(s.get("sun_elev")),
        s.get("occupied"),
        tuple(1 if z in (s.get("zones") or []) else 0 for z in zones),
        s.get("media"),
        _since_bucket(s.get("_since_active")),
    )


def vectorize(key: tuple) -> list[float]:
    tod, daytype, sun, occ, zbits, media, since = key
    ang = 2 * math.pi * tod / 1440.0
    unk = 0.5  # missing data sits in the middle, so it doesn't pull toward either side
    return [
        math.cos(ang), math.sin(ang),
        0.6 if daytype == "weekend" else 0.0,
        SUN_V.get(sun, unk) * 0.8,
        float(occ) if occ is not None else unk,
        *[0.8 * b for b in zbits],
        float(media) if media is not None else unk,
        SINCE_V.get(since, unk) * 0.8,
    ]


def _d2(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def kmeans(points: list[list[float]], weights: list[float], k: int, seed: int = 7, iters: int = 60):
    rnd = random.Random(seed)
    # k-means++ (weighted) initialisation
    cents = [points[rnd.choices(range(len(points)), weights=weights)[0]][:]]
    while len(cents) < k:
        d = [w * min(_d2(p, c) for c in cents) for p, w in zip(points, weights)]
        if sum(d) <= 0:
            break
        cents.append(points[rnd.choices(range(len(points)), weights=d)[0]][:])
    assign = [0] * len(points)
    for _ in range(iters):
        new = [min(range(len(cents)), key=lambda c: _d2(p, cents[c])) for p in points]
        moved = new != assign
        assign = new
        dims = len(points[0])
        sums = [[0.0] * dims for _ in cents]
        tot = [0.0] * len(cents)
        for p, w, a in zip(points, weights, assign):
            tot[a] += w
            for j in range(dims):
                sums[a][j] += w * p[j]
        cents = [[v / tot[c] for v in sums[c]] if tot[c] else cents[c] for c in range(len(cents))]
        if not moved:
            break
    return cents, assign


def silhouette(dist: list[list[float]], weights: list[float], assign: list[int]) -> float:
    groups: dict[int, list[int]] = defaultdict(list)
    for i, a in enumerate(assign):
        groups[a].append(i)
    if len(groups) < 2:
        return -1.0
    total, wsum = 0.0, 0.0
    for i, a in enumerate(assign):
        own = groups[a]
        w_own = sum(weights[j] for j in own) - weights[i]
        if w_own <= 0:
            continue  # singleton cluster point: silhouette 0 by convention
        ai = sum(weights[j] * dist[i][j] for j in own if j != i) / w_own
        bi = min(sum(weights[j] * dist[i][j] for j in g) / sum(weights[j] for j in g)
                 for c, g in groups.items() if c != a)
        total += weights[i] * ((bi - ai) / max(ai, bi) if max(ai, bi) > 0 else 0.0)
        wsum += weights[i]
    return total / wsum if wsum else -1.0


def _hour_ranges(hours: Counter, total: float) -> str:
    on = [h for h in range(24) if hours.get(h, 0) / total >= 0.05]
    if not on:
        return "any time"
    runs, start = [], on[0]
    for a, b in zip(on, on[1:] + [None]):
        if b != a + 1:
            runs.append((start, a))
            start = b
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == 23:  # wrap past midnight
        runs = [(runs[-1][0], runs[0][1])] + runs[1:-1]
    return ", ".join(f"{s:02d}–{(e + 1) % 24:02d}" for s, e in runs)


def describe(members: list[dict[str, Any]], zones: list[str]) -> str:
    n = len(members)
    hours = Counter(s["minute_of_day"] // 60 for s in members)
    days = Counter(s["daytype"] for s in members)
    parts = []
    dt, c = days.most_common(1)[0]
    parts.append(f"{dt}s" if c / n >= 0.8 else "any day")
    parts.append(_hour_ranges(hours, n))
    occ = sum(1 for s in members if s.get("occupied") == 1) / n
    zs = [z for z in zones if sum(1 for s in members if z in (s.get("zones") or [])) / n >= 0.6]
    if zs:
        parts.append("+".join(zs))
    elif occ >= 0.6:
        parts.append("someone home")
    elif occ <= 0.2:
        parts.append("nobody here")
    if sum(1 for s in members if s.get("media") == 1) / n >= 0.6:
        parts.append("TV on")
    since = Counter(_since_bucket(s.get("_since_active")) for s in members).most_common(1)[0]
    if since[0] and since[1] / n >= 0.6:
        parts.append({"now": "active", "<30m": "just quiet", "<2h": "quiet <2h", "<6h": "quiet <6h",
                      "6h+": "long quiet"}[since[0]])
    sun = Counter(_sun_bucket(s.get("sun_elev")) for s in members).most_common(1)[0]
    if sun[0] and sun[1] / n >= 0.7:
        parts.append({"night": "dark", "twilight": "twilight", "low": "low sun", "high": "daylight"}[sun[0]])
    return " · ".join(parts)


def light_behaviour(members: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    roles = sorted({r for s in members for r in (s.get("lights") or {})})
    for r in roles:
        vals = [(s.get("lights") or {}).get(r) for s in members]
        known = [v for v in vals if v is not None]
        if not known:
            continue
        on = [v for v in known if v > 0]
        ks = [(s.get("kelvin") or {}).get(r) for s in members if ((s.get("lights") or {}).get(r) or 0) > 0]
        ks = [k for k in ks if k]
        # The second strand: inside a state, brightness as a function of daylight (sun bucket)
        by_light: dict[str, list[float]] = defaultdict(list)
        for s in members:
            v = (s.get("lights") or {}).get(r)
            b = _sun_bucket(s.get("sun_elev"))
            if v is not None and v > 0 and b:
                by_light[b].append(v)
        out[r] = {
            "on_ratio": round(len(on) / len(known), 2),
            "median_when_on": round(median(on), 1) if on else None,
            "median_kelvin": int(median(ks)) if ks else None,
            "by_daylight": {b: round(median(v), 1) for b, v in by_light.items() if len(v) >= 3},
            "samples": len(known),
        }
    return out


def keys_match(a: list[list[float]], b: list[list[float]]) -> bool:
    """Two states are 'the same' if any of their member centres are close. Survives renumbering + merges."""
    return any(len(x) == len(y) and math.sqrt(_d2(x, y)) < NAME_MATCH_DISTANCE for x in a for y in b)


def _entry_keys(entry: dict[str, Any]) -> list[list[float]]:
    return entry.get("keys") or ([entry["centroid"]] if entry.get("centroid") else [])


def discover_states(slots: list[dict[str, Any]], max_states: int = 28,
                    named: list[dict[str, Any]] | None = None,
                    merges: list[list[list[float]]] | None = None,
                    links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Cluster contexts into states. Writes slot['state'] and returns the state model."""
    usable = [s for s in slots if s.get("occupied") is not None or s.get("light_max") is not None]
    if len(usable) < 288:  # less than a day of data: nothing worth discovering yet
        return {"states": [], "k": 0, "reason": "need at least a day of history"}
    zones = sorted({z for s in usable for z in (s.get("zones") or [])})
    keys = Counter(context_key(s, zones) for s in usable)
    uniq = list(keys)
    pts = [vectorize(k) for k in uniq]
    w = [float(keys[k]) for k in uniq]
    total = sum(w)
    min_w = max(12.0, 0.01 * total)
    dist = [[math.sqrt(_d2(a, b)) for b in pts] for a in pts]

    best_score, candidates = -1.0, []
    upper = min(max_states, max(2, len(uniq) - 1))
    ks = sorted({k for k in (2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 24, 28) if k <= upper} | {upper})
    for k in ks:
        cents, assign = kmeans(pts, w, k)
        sizes = Counter()
        for a, wt in zip(assign, w):
            sizes[a] += wt
        if min(sizes.values()) < min_w:
            continue
        score = silhouette(dist, w, assign)
        candidates.append((k, score, cents, assign))
        best_score = max(best_score, score)
    if not candidates:
        return {"states": [], "k": 0, "reason": "data too uniform to split"}
    k, score, cents, assign = max((c for c in candidates if c[1] >= 0.9 * best_score), key=lambda c: c[0])

    # Your merges: clusters whose centre matches any centre in a merge group become one state
    parent = list(range(len(cents)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for group in merges or []:
        hit = [c for c in range(len(cents)) if keys_match([cents[c]], group)]
        for c in hit[1:]:
            parent[find(c)] = find(hit[0])
    member_keys: dict[int, list[list[float]]] = defaultdict(list)
    for c in range(len(cents)):
        member_keys[find(c)].append([round(v, 4) for v in cents[c]])
    assign = [find(a) for a in assign]
    wsum: dict[int, float] = defaultdict(float)
    acc: dict[int, list[float]] = {}
    for pnt, wt, a in zip(pts, w, assign):
        wsum[a] += wt
        acc.setdefault(a, [0.0] * len(pnt))
        for j, v in enumerate(pnt):
            acc[a][j] += wt * v
    cents = {a: [v / wsum[a] for v in acc[a]] for a in acc}

    key_state = {key: a for key, a in zip(uniq, assign)}
    members: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for s in slots:
        s["state"] = None
    for s in usable:
        sid = key_state[context_key(s, zones)]
        s["state"] = sid
        members[sid].append(s)

    # order states by time of day of their centre of mass, so ids read roughly morning -> night
    def tod(sid: int) -> float:
        c = cents[sid]
        return (math.atan2(c[1], c[0]) % (2 * math.pi)) / (2 * math.pi) * 24

    order = sorted(members, key=tod)
    remap = {old: new for new, old in enumerate(order)}
    for s in usable:
        s["state"] = remap[s["state"]]

    states = []
    for old in order:
        mem = members[old]
        intents = Counter(s.get("intent") for s in mem if s.get("intent"))
        top_intent, top_n = intents.most_common(1)[0] if intents else (None, 0)
        cent = cents[old]
        keys = member_keys[old]
        name = next((nm["name"] for nm in named or [] if keys_match(keys, _entry_keys(nm))), None)
        state_links = {}
        for ln in links or []:
            if keys_match(keys, _entry_keys(ln)):
                state_links[ln["role"]] = {k: ln.get(k) for k in ("mode", "brightness", "kelvin", "locked")}
        states.append({
            "id": remap[old],
            "name": name,
            "description": describe(mem, zones),
            "slots": len(mem),
            "share": round(len(mem) / len(usable), 3),
            "intent": top_intent,
            "intent_purity": round(top_n / sum(intents.values()), 2) if intents else None,
            "hours": [sum(1 for s in mem if s["minute_of_day"] // 60 == h) for h in range(24)],
            "lights": light_behaviour(mem),
            "centroid": [round(v, 4) for v in cent],
            "keys": keys,
            "merged_from": len(keys),
            "links": state_links,
        })
    return {"k": k, "silhouette": round(score, 3), "zones": zones, "states": states,
            "tried": [{"k": c[0], "silhouette": round(c[1], 3)} for c in candidates]}


def assign_state(model: dict[str, Any], s: dict[str, Any]) -> int | None:
    """Nearest discovered state for a live context."""
    if not model.get("states"):
        return None
    v = vectorize(context_key(s, model.get("zones", [])))
    return min(model["states"], key=lambda st: _d2(v, st["centroid"]))["id"]
