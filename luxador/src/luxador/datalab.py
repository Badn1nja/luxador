"""Phase 1 · Data: traceback of past light levels, the 5-minute grid, intent labels, and the intent model.

See docs/phase1-data.md for the design. Everything here is read-only with respect to the lights.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from statistics import median
from typing import Any, Iterable

from .models import RoomConfig
from .states import assign_state, discover_states

SLOT_MINUTES = 5
SLOT = timedelta(minutes=SLOT_MINUTES)
INTENTS = ["wake", "sleep", "nightlight", "evening", "day", "away"]
NIGHTLIGHT_MAX_PCT = 15
SLEEP_MIN_SLOTS = 36  # 3 h
WAKE_SLOTS = 6  # 30 min
_ON_WORDS = {"on", "home", "detected", "occupied", "open", "true"}
_KEEP_ATTRS = ("brightness", "color_temp_kelvin", "rgb_color", "elevation")

# --------------------------------------------------------------------------- time helpers

def get_tz(name: str | None) -> tzinfo:
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_ts(v: str) -> datetime:
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def floor_slot(dt: datetime) -> datetime:
    return dt.replace(minute=dt.minute - dt.minute % SLOT_MINUTES, second=0, microsecond=0)


# --------------------------------------------------------------------------- 1.1 traceback import

@dataclass
class RoomEntities:
    lights: dict[str, list[str]]
    presence: list[str]
    zones: dict[str, list[str]]
    lux_in: str | None
    lux_out: str | None
    media: str | None
    sun: str | None

    def all(self) -> list[str]:
        ids: set[str] = set(self.presence)
        for ents in self.lights.values():
            ids.update(ents)
        for ents in self.zones.values():
            ids.update(ents)
        ids.update(e for e in (self.lux_in, self.lux_out, self.media, self.sun) if e)
        return sorted(ids)


def room_entities(cfg: RoomConfig, outdoor_lux: str | None = None, sun: str | None = "sun.sun") -> RoomEntities:
    return RoomEntities(
        lights={r: list(e) for r, e in cfg.bulbs.items()},
        presence=list(cfg.presence.sensors),
        zones={z: list(zc.presence) for z, zc in cfg.presence.zones.items()},
        lux_in=cfg.lux_sensor,
        lux_out=outdoor_lux or None,
        media=cfg.media_sensor,
        sun=sun or None,
    )


def normalize_history(raw: Iterable[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
    """HA history state objects -> (entity_id, utc_iso_ts, state, attrs_json) keeping only useful attributes.

    HA's minimal responses omit entity_id/attributes after the first item, so carry the entity forward.
    """
    out = []
    last_entity = None
    for st in raw:
        ent = st.get("entity_id") or last_entity
        last_entity = ent
        ts = st.get("last_changed") or st.get("last_updated")
        if not ent or not ts:
            continue
        attrs = st.get("attributes") or {}
        keep = {k: attrs[k] for k in _KEEP_ATTRS if attrs.get(k) is not None}
        out.append((ent, parse_ts(ts).astimezone(timezone.utc).isoformat(), str(st.get("state")), json.dumps(keep)))
    return out


def normalize_statistics(stats: dict[str, list[dict[str, Any]]]) -> list[tuple[str, str, Any, Any, Any]]:
    rows = []
    for sid, points in (stats or {}).items():
        for p in points or []:
            start = p.get("start")
            if isinstance(start, (int, float)):  # newer HA: epoch milliseconds
                ts = datetime.fromtimestamp(start / 1000.0, tz=timezone.utc)
            elif isinstance(start, str):
                ts = parse_ts(start)
            else:
                continue
            rows.append((sid, ts.isoformat(), p.get("mean"), p.get("min"), p.get("max")))
    return rows


# --------------------------------------------------------------------------- 1.2 the grid

class Series:
    """A step function: each entity's state holds until it changes."""

    def __init__(self, rows: list[tuple[str, str, str]]):  # (ts_iso, state, attrs_json)
        rows = sorted(rows)
        self.times = [parse_ts(r[0]) for r in rows]
        self.states = [r[1] for r in rows]
        self.attrs = [json.loads(r[2] or "{}") for r in rows]

    def idx(self, t: datetime) -> int:
        return bisect_right(self.times, t) - 1

    def state_at(self, t: datetime) -> tuple[str | None, dict]:
        i = self.idx(t)
        return (self.states[i], self.attrs[i]) if i >= 0 else (None, {})

    def any_on(self, start: datetime, end: datetime) -> bool | None:
        """Was the entity on at any point in [start, end)? None if it was never known in that window."""
        i = self.idx(start)
        states = [self.states[i]] if i >= 0 else []
        states += self.states[bisect_right(self.times, start):bisect_left(self.times, end)]
        known = [s for s in states if s not in ("unavailable", "unknown")]
        if not known:
            return None
        return any(s.lower() in _ON_WORDS for s in known)


def _bri_pct(state: str | None, attrs: dict) -> float | None:
    if state is None or state in ("unavailable", "unknown"):
        return None
    if state != "on":
        return 0.0
    b = attrs.get("brightness")
    return round(float(b) / 2.55, 1) if b is not None else 100.0


def _num(state: str | None) -> float | None:
    try:
        return float(state) if state is not None else None
    except ValueError:
        return None


def build_slots(
    ents: RoomEntities,
    history: dict[str, list[tuple[str, str, str]]],
    start: datetime,
    end: datetime,
    tz: tzinfo,
) -> list[dict[str, Any]]:
    """Resample raw history into one row per 5 minutes (local time). Missing stays None."""
    series = {e: Series(rows) for e, rows in history.items() if rows}
    out = []
    t = floor_slot(start.astimezone(tz))
    end_local = end.astimezone(tz)
    while t < end_local:
        t2 = t + SLOT
        mid = (t + SLOT / 2).astimezone(timezone.utc)
        s_utc, e_utc = t.astimezone(timezone.utc), t2.astimezone(timezone.utc)

        def at(e: str | None) -> tuple[str | None, dict]:
            return series[e].state_at(mid) if e and e in series else (None, {})

        lights: dict[str, float | None] = {}
        kelvin: dict[str, int | None] = {}
        for role, ents_ in ents.lights.items():
            reads = [at(e) for e in ents_]
            vals = [_bri_pct(*r) for r in reads]
            known = [v for v in vals if v is not None]
            lights[role] = max(known) if known else None
            ks = [r[1].get("color_temp_kelvin") for r, v in zip(reads, vals) if v and r[1].get("color_temp_kelvin")]
            kelvin[role] = int(ks[0]) if ks else None
        known_lights = [v for v in lights.values() if v is not None]

        pres = [series[e].any_on(s_utc, e_utc) for e in ents.presence if e in series]
        zones = {}
        for z, zents in ents.zones.items():
            zv = [series[e].any_on(s_utc, e_utc) for e in zents if e in series]
            zones[z] = any(v for v in zv if v) if any(v is not None for v in zv) else None
        all_pres = [v for v in pres + list(zones.values()) if v is not None]
        occupied = (1 if any(all_pres) else 0) if all_pres else None

        media_state = at(ents.media)[0]
        media = None if media_state in (None, "unavailable", "unknown") else (1 if media_state in ("playing", "buffering") else 0)
        sun_state, sun_attrs = at(ents.sun)
        sun_elev = sun_attrs.get("elevation") if sun_state is not None else None

        out.append({
            "slot": t.strftime("%Y-%m-%dT%H:%M"),
            "minute_of_day": t.hour * 60 + t.minute,
            "weekday": t.weekday(),
            "daytype": "weekend" if t.weekday() >= 5 else "weekday",
            "lux_in": _num(at(ents.lux_in)[0]),
            "lux_out": _num(at(ents.lux_out)[0]),
            "sun_elev": sun_elev,
            "occupied": occupied,
            "zones": [z for z, v in zones.items() if v],
            "media": media,
            "lights": lights,
            "kelvin": kelvin,
            "light_max": max(known_lights) if known_lights else None,
        })
        t = t2
    return out


# --------------------------------------------------------------------------- 1.3 labels

def _active(s: dict[str, Any]) -> bool:
    return s.get("occupied") == 1 or (s.get("light_max") or 0) > 0 or s.get("media") == 1


def label_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rule labels: sleep / nightlight / wake / evening / day / away. They name the discovered states' intents."""
    for s in slots:
        s["intent"], s["intent_source"] = None, None
    keys = [s["slot"] for s in slots]

    # Sleep stretches: per night window 19:00 -> 11:00, the longest run with no light > 15 % and no TV
    days = sorted({k[:10] for k in keys})
    for d in days:
        w_start = f"{d}T19:00"
        w_end = (datetime.fromisoformat(d) + timedelta(days=1)).strftime("%Y-%m-%dT11:00")
        best, run_start = (0, -1, -1), None
        i = bisect_left(keys, w_start)
        while i < len(slots) and slots[i]["slot"] < w_end:
            s = slots[i]
            quiet = s.get("light_max") is not None and s["light_max"] <= NIGHTLIGHT_MAX_PCT and s.get("media") != 1
            if quiet:
                run_start = i if run_start is None else run_start
                if i - run_start + 1 > best[0]:
                    best = (i - run_start + 1, run_start, i + 1)
            else:
                run_start = None
            i += 1
        length, a, b = best
        if length < SLEEP_MIN_SLOTS:
            continue
        for k in range(a, b):
            slots[k]["intent"] = "nightlight" if (slots[k]["light_max"] or 0) > 0 else "sleep"
            slots[k]["intent_source"] = "rule"
        # Wake: the stretch ended inside the window (something switched on) between 04:00 and 12:00
        if b < len(slots) and slots[b]["slot"] < w_end and 4 * 60 <= slots[b]["minute_of_day"] < 12 * 60:
            for k in range(b, min(len(slots), b + WAKE_SLOTS)):
                if slots[k]["intent"] is None:
                    slots[k]["intent"], slots[k]["intent_source"] = "wake", "rule"

    for s in slots:
        if s["intent"] is not None:
            continue
        if s.get("light_max") is None and s.get("occupied") is None:
            continue  # no data, no label
        if s.get("occupied") == 0 and not (s.get("light_max") or 0) and s.get("media") != 1:
            s["intent"] = "away"
        elif _active(s):
            elev = s.get("sun_elev")
            m = s["minute_of_day"]
            dark = elev < 0 if elev is not None else (m >= 18 * 60 or m < 5 * 60)
            # A dark winter *morning* is not evening: evening = dark and after 14:00 (or the small hours)
            s["intent"] = "evening" if dark and (m >= 14 * 60 or m < 4 * 60) else "day"
        else:
            continue
        s["intent_source"] = "rule"

    return slots


# --------------------------------------------------------------------------- 1.4 activity clock

def add_since_active(slots: list[dict[str, Any]]) -> None:
    """Minutes since the room was last active (presence, light or TV); a context feature for states."""
    last = None
    for s in slots:
        t = datetime.fromisoformat(s["slot"])
        if _active(s):
            last = t
        s["_since_active"] = 0.0 if last == t else ((t - last).total_seconds() / 60.0 if last else None)


# --------------------------------------------------------------------------- predictions & traceback

def _pct(vals: list[float], q: float) -> float:
    v = sorted(vals)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


def _hhmm(m: float) -> str:
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def sleep_wake_times(slots: list[dict[str, Any]]) -> dict[str, Any]:
    nights = []
    i = 0
    while i < len(slots):
        if slots[i].get("intent") in ("sleep", "nightlight"):
            j = i
            while j < len(slots) and slots[j].get("intent") in ("sleep", "nightlight"):
                j += 1
            if j - i >= SLEEP_MIN_SLOTS:
                start = datetime.fromisoformat(slots[i]["slot"])
                end = datetime.fromisoformat(slots[j - 1]["slot"]) + SLOT
                woke = j < len(slots) and slots[j].get("intent") == "wake"
                nights.append({
                    "date": end.strftime("%Y-%m-%d"),
                    "daytype": "weekend" if end.weekday() >= 5 else "weekday",
                    "sleep_at": start.strftime("%H:%M"),
                    "wake_at": end.strftime("%H:%M") if woke else None,
                    "hours": round((end - start).total_seconds() / 3600, 1),
                })
            i = j
        else:
            i += 1

    def stats(key: str, daytype: str) -> dict[str, Any] | None:
        mins = []
        for n in nights:
            if n["daytype"] != daytype or not n[key]:
                continue
            h, m = map(int, n[key].split(":"))
            v = h * 60 + m
            mins.append(v + 1440 if key == "sleep_at" and v < 12 * 60 else v)  # bedtimes after midnight
        if not mins:
            return None
        return {"n": len(mins), "median": _hhmm(median(mins)), "p10": _hhmm(_pct(mins, 0.1)), "p90": _hhmm(_pct(mins, 0.9))}

    return {
        "nights": nights,
        "wake": {d: stats("wake_at", d) for d in ("weekday", "weekend")},
        "sleep": {d: stats("sleep_at", d) for d in ("weekday", "weekend")},
    }


def traceback_profile(slots: list[dict[str, Any]]) -> dict[str, Any]:
    """Sub-phase 1: what the lights actually were, by hour x weekday, role x hour x daytype, role x intent."""
    heat: dict[tuple[int, int], list[float]] = defaultdict(list)
    role_hour: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    role_intent: dict[tuple[str, str], list[float]] = defaultdict(list)
    for s in slots:
        if s.get("light_max") is not None:
            heat[(s["weekday"], s["minute_of_day"] // 60)].append(s["light_max"])
        for role, v in (s.get("lights") or {}).items():
            if v is None:
                continue
            role_hour[(role, s["daytype"], s["minute_of_day"] // 60)].append(v)
            if s.get("intent") and v > 0:
                role_intent[(role, s["intent"])].append(v)
    heatmap = [[round(median(heat[(d, h)]), 1) if heat.get((d, h)) else None for h in range(24)] for d in range(7)]
    roles: dict[str, Any] = defaultdict(lambda: {"weekday": [None] * 24, "weekend": [None] * 24, "on_ratio": {}})
    for (role, dt, h), vals in role_hour.items():
        roles[role][dt][h] = round(median(vals), 1)
        roles[role]["on_ratio"].setdefault(dt, [None] * 24)[h] = round(sum(1 for v in vals if v > 0) / len(vals), 2)
    by_intent: dict[str, dict[str, Any]] = defaultdict(dict)
    for (role, it), vals in role_intent.items():
        by_intent[role][it] = {"median_when_on": round(median(vals), 1), "n": len(vals)}
    return {"heatmap_weekday_hour": heatmap, "roles": dict(roles), "by_intent": dict(by_intent)}


def lux_profile(slots: list[dict[str, Any]], lts_rows: list[dict[str, Any]], lux_in: str | None, lux_out: str | None,
                tz: tzinfo) -> dict[str, Any]:
    per_hour: dict[str, list[list[float]]] = {k: [[] for _ in range(24)] for k in ("lux_in", "lux_out", "sun_elev")}
    for s in slots:
        h = s["minute_of_day"] // 60
        for k in per_hour:
            if s.get(k) is not None:
                per_hour[k][h].append(float(s[k]))
    hourly = {k: [round(median(v), 1) if v else None for v in rows] for k, rows in per_hour.items()}
    seasonal: dict[str, dict[str, list]] = {}
    for sid, label in ((lux_in, "lux_in"), (lux_out, "lux_out")):
        if not sid:
            continue
        cells: dict[tuple[int, int], list[float]] = defaultdict(list)
        for r in lts_rows:
            if r["statistic_id"] == sid and r["mean"] is not None:
                t = parse_ts(r["hour_ts"]).astimezone(tz)
                cells[(t.month, t.hour)].append(float(r["mean"]))
        if cells:
            seasonal[label] = {str(m): [round(median(cells[(m, h)]), 1) if cells.get((m, h)) else None for h in range(24)]
                               for m in sorted({m for m, _ in cells})}
    return {"hourly": hourly, "seasonal": seasonal}


def coverage(slots: list[dict[str, Any]]) -> dict[str, Any]:
    if not slots:
        return {"slots": 0}
    n = len(slots)
    missing = {k: round(sum(1 for s in slots if s.get(k) is None) / n, 3)
               for k in ("lux_in", "lux_out", "sun_elev", "occupied", "media", "light_max")}
    labels = Counter((s.get("intent") or "unlabelled", s.get("intent_source") or "-") for s in slots)
    return {
        "slots": n,
        "first": slots[0]["slot"],
        "last": slots[-1]["slot"],
        "days": round(n * SLOT_MINUTES / 1440, 1),
        "missing_share": missing,
        "labels": [{"intent": i, "source": src, "slots": c} for (i, src), c in labels.most_common()],
    }


# --------------------------------------------------------------------------- seasonal (yearly) memory

def seasonal_cells(slots: list[dict[str, Any]]) -> dict[tuple[int, int, str], dict[str, Any]]:
    """Sum slots into (ISO week, hour, daytype) cells. Sums + counts, so cells merge across years."""
    cells: dict[tuple[int, int, str], dict[str, Any]] = {}
    for s in slots:
        t = datetime.fromisoformat(s["slot"])
        key = (t.isocalendar().week, t.hour, s["daytype"])
        c = cells.setdefault(key, {"n": 0, "occ_n": 0, "occ_known": 0, "light_on": 0, "light_bri_sum": 0.0,
                                   "lux_in_sum": 0.0, "lux_in_n": 0, "sun_sum": 0.0, "sun_n": 0,
                                   "roles": defaultdict(lambda: [0, 0.0, 0]), "intents": Counter(), "years": set()})
        c["n"] += 1
        c["years"].add(t.year)
        if s.get("occupied") is not None:
            c["occ_known"] += 1
            c["occ_n"] += int(s["occupied"])
        lm = s.get("light_max")
        if lm:
            c["light_on"] += 1
            c["light_bri_sum"] += lm
        if s.get("lux_in") is not None:
            c["lux_in_sum"] += s["lux_in"]
            c["lux_in_n"] += 1
        if s.get("sun_elev") is not None:
            c["sun_sum"] += s["sun_elev"]
            c["sun_n"] += 1
        for r, v in (s.get("lights") or {}).items():
            if v is None:
                continue
            rr = c["roles"][r]
            rr[2] += 1
            if v > 0:
                rr[0] += 1
                rr[1] += v
        if s.get("intent"):
            c["intents"][s["intent"]] += 1
    for c in cells.values():
        c["roles"] = dict(c["roles"])
        c["intents"] = dict(c["intents"])
        c["years"] = sorted(c["years"])
    return cells


def seasonal_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per ISO week: how often someone was home, how often lights were on and how bright, mean sun."""
    weeks: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        w = weeks[r["week"]]
        for k in ("n", "occ_n", "occ_known", "light_on", "light_bri_sum", "sun_sum", "sun_n"):
            w[k] += r[k]
    out = []
    for wk in sorted(weeks):
        w = weeks[wk]
        out.append({
            "week": wk,
            "slots": int(w["n"]),
            "home_share": round(w["occ_n"] / w["occ_known"], 2) if w["occ_known"] else None,
            "lights_on_share": round(w["light_on"] / w["n"], 2) if w["n"] else None,
            "bri_when_on": round(w["light_bri_sum"] / w["light_on"], 1) if w["light_on"] else None,
            "sun_mean": round(w["sun_sum"] / w["sun_n"], 1) if w["sun_n"] else None,
        })
    years = sorted({y for r in rows for y in json.loads(r["years_json"])})
    return {"weeks": out, "weeks_covered": len(out), "years": years}


# --------------------------------------------------------------------------- orchestration

class DataLab:
    """Runs the Phase 1 pipeline for every room: import -> 5-minute grid -> labels -> discovered states."""

    CAPTURE_MIN_SECONDS = 60  # numeric sensors (lux) are thinned to one row a minute unless they jump

    def __init__(self, store: Any, rooms: dict[str, RoomConfig], settings: dict[str, Any], bridge: Any) -> None:
        self.store = store
        self.rooms = rooms
        self.settings = settings
        self.bridge = bridge
        self.status: dict[str, Any] = {"state": "idle"}
        self._last_capture: dict[str, tuple[float, float | None]] = {}

    # ---- config
    def tz(self) -> tzinfo:
        return get_tz(self.settings.get("time_zone") or getattr(self.bridge, "time_zone", None))

    def entities(self, room: str) -> RoomEntities:
        return room_entities(self.rooms[room], self.settings.get("outdoor_lux_sensor"),
                             self.settings.get("sun_entity", "sun.sun"))

    def tracked(self) -> set[str]:
        out: set[str] = set()
        for r in self.rooms:
            out.update(self.entities(r).all())
        return out

    # ---- live capture
    def capture(self, entity_id: str, state_obj: dict[str, Any]) -> None:
        if entity_id not in self.tracked():
            return
        rows = normalize_history([{**state_obj, "entity_id": entity_id}])
        if not rows:
            return
        val = _num(rows[0][2])
        if val is not None and not entity_id.startswith("light."):
            ts = parse_ts(rows[0][1]).timestamp()
            last = self._last_capture.get(entity_id)
            if last and ts - last[0] < self.CAPTURE_MIN_SECONDS and last[1] is not None and \
                    abs(val - last[1]) <= max(5.0, abs(last[1]) * 0.25):
                return
            self._last_capture[entity_id] = (ts, val)
        self.store.store_history(rows, source="live")

    # ---- import
    async def import_history(self, days: int = 10, stats_days: int = 400) -> dict[str, Any]:
        if not self.bridge.configured:
            self.status = {"state": "error", "error": "Home Assistant is not connected"}
            return self.status
        self.status = {"state": "importing", "days": days}
        try:
            end = datetime.now(timezone.utc)
            ents = sorted(self.tracked())
            raw = await self.bridge.fetch_history(ents, end - timedelta(days=days), end)
            n_hist = self.store.store_history(normalize_history(raw))
            lux_ids = sorted({e for r in self.rooms for e in (self.entities(r).lux_in, self.entities(r).lux_out) if e})
            n_stats, stats_error = 0, None
            try:
                stats = await self.bridge.fetch_statistics(lux_ids, end - timedelta(days=stats_days), end)
                n_stats = self.store.store_statistics(normalize_statistics(stats))
            except Exception as exc:  # stats are a bonus; history alone is enough to continue
                stats_error = str(exc)
            rebuilt = {r: self.rebuild(r) for r in self.rooms}
            self.status = {"state": "done", "history_rows_added": n_hist, "statistics_rows": n_stats,
                           "statistics_error": stats_error, "rooms": rebuilt,
                           "finished": datetime.now().isoformat(timespec="seconds")}
        except Exception as exc:
            self.status = {"state": "error", "error": str(exc)}
        return self.status

    # ---- grid + model
    def detail_days(self) -> int:
        return int(self.settings.get("detail_days", 90) or 90)

    def fold_aged_slots(self, room: str, cutoff_slot: str) -> int:
        """Move grid slots older than the detail window into the permanent seasonal table (once each)."""
        meta = self.store.load_model(room, "seasonal_meta")
        folded_until = json.loads(meta[1]).get("folded_until", "") if meta else ""
        old = [s for s in self.store.load_slots(room) if folded_until <= s["slot"] < cutoff_slot]
        if old:
            self.store.merge_seasonal(room, seasonal_cells(old))
            self.store.save_model(room, "seasonal_meta", json.dumps({"folded_until": cutoff_slot}))
        return len(old)

    def rebuild(self, room: str) -> dict[str, Any]:
        ents = self.entities(room)
        span = self.store.history_span()
        if not span["first"]:
            return {"slots": 0}
        end = datetime.now(timezone.utc)
        window_start = end - timedelta(days=self.detail_days())
        start = max(parse_ts(span["first"]), window_start)
        self.fold_aged_slots(room, floor_slot(start.astimezone(self.tz())).strftime("%Y-%m-%dT%H:%M"))
        hist = self.store.load_history(ents.all(), since_iso=start.isoformat())
        slots = build_slots(ents, hist, start, end, self.tz())
        label_slots(slots)
        add_since_active(slots)
        state_model = discover_states(slots, int(self.settings.get("max_states", 28) or 28),
                                      named=self.state_names(room), merges=self._get(room, "state_merges"),
                                      links=self._get(room, "state_links"))
        self.store.replace_slots(room, slots)
        self.store.save_model(room, "states", json.dumps(state_model))
        cov = coverage(slots)
        cov["states"] = state_model.get("k", 0)
        return cov

    def states(self, room: str) -> dict[str, Any]:
        m = self.store.load_model(room, "states")
        return json.loads(m[1]) if m else {"states": [], "k": 0}

    def _get(self, room: str, kind: str) -> list:
        m = self.store.load_model(room, kind)
        return json.loads(m[1]) if m else []

    def _put(self, room: str, kind: str, data: list) -> None:
        self.store.save_model(room, kind, json.dumps(data))

    def state_names(self, room: str) -> list[dict[str, Any]]:
        return self._get(room, "state_names")

    def _state(self, room: str, state_id: int) -> dict[str, Any]:
        st = next((x for x in self.states(room).get("states", []) if x["id"] == state_id), None)
        if st is None:
            raise KeyError(state_id)
        return st

    def name_state(self, room: str, state_id: int, name: str | None) -> dict[str, Any]:
        """Names stick to a state's member centres, so they survive rebuilds that renumber states."""
        from .states import keys_match, _entry_keys

        model = self.states(room)
        st = next((x for x in model.get("states", []) if x["id"] == state_id), None)
        if st is None:
            raise KeyError(state_id)
        keys = st.get("keys") or [st["centroid"]]
        names = [n for n in self.state_names(room) if not keys_match(keys, _entry_keys(n))]
        clean = (name or "").strip()
        if clean:
            names.append({"keys": keys, "name": clean})
        self._put(room, "state_names", names)
        st["name"] = clean or None
        self.store.save_model(room, "states", json.dumps(model))
        return st

    # ---- switchboard: merge / unmerge / link
    def merge_states(self, room: str, ids: list[int]) -> dict[str, Any]:
        from .states import keys_match

        if len(set(ids)) < 2:
            raise ValueError("pick at least two states to merge")
        keys = [k for i in ids for k in (self._state(room, i).get("keys") or [self._state(room, i)["centroid"]])]
        groups = [g for g in self._get(room, "state_merges") if not keys_match(g, keys)]
        groups.append(keys)  # absorbs any earlier group these states were part of
        self._put(room, "state_merges", groups)
        self.rebuild(room)
        return self.states(room)

    def unmerge_state(self, room: str, state_id: int) -> dict[str, Any]:
        from .states import keys_match

        keys = self._state(room, state_id).get("keys") or []
        self._put(room, "state_merges", [g for g in self._get(room, "state_merges") if not keys_match(g, keys)])
        self.rebuild(room)
        return self.states(room)

    def link_state(self, room: str, state_id: int, role: str, mode: str,
                   brightness: float | None = None, kelvin: int | None = None,
                   locked: bool | None = None) -> dict[str, Any]:
        """How a bulb behaves in a state: learned (default), pinned (fixed level), off, or ignore (hands off).

        A locked link can't be changed (by you or any future auto-learning) until it is unlocked:
        send the same mode with locked=False first.
        """
        from .states import keys_match, _entry_keys

        st0 = self._state(room, state_id)
        existing = next((ln for ln in self._get(room, "state_links")
                         if ln["role"] == role and keys_match(st0.get("keys") or [st0["centroid"]], _entry_keys(ln))), None)
        if existing and existing.get("locked") and locked is not False:
            raise PermissionError(f"{role} is locked in this state (unlock first)")
        is_locked = bool(locked) if locked is not None else bool(existing and existing.get("locked"))

        if mode not in ("learned", "pinned", "off", "ignore"):
            raise ValueError("mode must be learned, pinned, off or ignore")
        if role not in self.rooms[room].bulbs:
            raise ValueError(f"unknown light group {role!r}")
        if mode == "pinned" and brightness is None:
            raise ValueError("pinned needs a brightness")
        st = self._state(room, state_id)
        keys = st.get("keys") or [st["centroid"]]
        links = [ln for ln in self._get(room, "state_links")
                 if not (ln["role"] == role and keys_match(keys, _entry_keys(ln)))]
        if mode != "learned" or is_locked:  # plain learned is the default, so it needs no row
            links.append({"keys": keys, "role": role, "mode": mode,
                          "brightness": max(0.0, min(100.0, float(brightness))) if brightness is not None else None,
                          "kelvin": int(kelvin) if kelvin else None, "locked": is_locked})
        self._put(room, "state_links", links)
        model = self.states(room)
        for x in model.get("states", []):
            if x["id"] == state_id:
                x.setdefault("links", {})
                if mode == "learned" and not is_locked:
                    x["links"].pop(role, None)
                else:
                    x["links"][role] = {"mode": mode, "brightness": links[-1]["brightness"],
                                        "kelvin": links[-1]["kelvin"], "locked": is_locked}
        self.store.save_model(room, "states", json.dumps(model))
        return self._state(room, state_id)

    def state_dataset(self, room: str, state_id: int) -> dict[str, Any]:
        """Everything about one state: when (hour, weekday, dates), daylight (sun, lux) and what each bulb did."""
        st = self._state(room, state_id)
        mem = [s for s in self.store.load_slots(room) if s.get("state") == state_id]
        hours = [0] * 24
        weekdays = [0] * 7
        sun: Counter = Counter()
        lux_by_hour: list[list[float]] = [[] for _ in range(24)]
        dates: Counter = Counter()
        for s in mem:
            h = s["minute_of_day"] // 60
            hours[h] += 1
            weekdays[s["weekday"]] += 1
            e = s.get("sun_elev")
            sun["night" if e is None or e < -6 else "twilight" if e < 0 else "low" if e < 15 else "high"] += 1
            if s.get("lux_in") is not None:
                lux_by_hour[h].append(s["lux_in"])
            dates[s["slot"][:10]] += 1
        recent = sorted(dates)[-21:]
        n = len(mem) or 1
        return {
            "state": st,
            "slots": len(mem),
            "hours": hours,
            "weekdays": weekdays,
            "sun": {k: round(v / n, 2) for k, v in sun.items()},
            "lux_in_by_hour": [round(median(v), 1) if v else None for v in lux_by_hour],
            "recent_days": [{"date": d, "minutes": dates[d] * SLOT_MINUTES} for d in recent],
            "roles": list(self.rooms[room].bulbs),
        }

    def live_state(self, room: str, occupied: bool, media_playing: bool, zones: dict[str, bool] | None,
                   since_active_min: float | None, now: datetime | None = None) -> dict[str, Any] | None:
        """Which discovered state the room is in right now (nearest centre in context space), or None."""
        model = self.states(room)
        if not model.get("states"):
            return None
        now = (now or datetime.now(timezone.utc)).astimezone(self.tz())
        states = getattr(self.bridge, "states", {}) or {}
        sun = ((states.get(self.settings.get("sun_entity", "sun.sun")) or {}).get("attributes") or {}).get("elevation")
        ctx = {"minute_of_day": now.hour * 60 + now.minute, "daytype": "weekend" if now.weekday() >= 5 else "weekday",
               "sun_elev": sun, "occupied": 1 if occupied else 0, "media": 1 if media_playing else 0,
               "zones": [z for z, on in (zones or {}).items() if on], "_since_active": since_active_min}
        sid = assign_state(model, ctx)
        st = next((x for x in model["states"] if x["id"] == sid), None)
        return {k: st.get(k) for k in ("id", "name", "description", "intent", "lights", "links")} if st else None

    def summary(self, room: str) -> dict[str, Any]:
        slots = self.store.load_slots(room)
        ents = self.entities(room)
        add_since_active(slots)
        return {
            "history": self.store.history_span(),
            "coverage": coverage(slots),
            "traceback": traceback_profile(slots),
            "lux": lux_profile(slots, self.store.load_statistics([e for e in (ents.lux_in, ents.lux_out) if e]),
                               ents.lux_in, ents.lux_out, self.tz()),
            "states": self.states(room),
            "seasonal": seasonal_summary(self.store.load_seasonal(room)),
            "detail_days": self.detail_days(),
            "sleep_wake": sleep_wake_times(slots),
            "entities": {"lux_in": ents.lux_in, "lux_out": ents.lux_out, "sun": ents.sun,
                         "tracked": len(ents.all())},
            "status": self.status,
        }


# --------------------------------------------------------------------------- time series for the dashboard

RANGES = {"today": (None, 5), "week": (7, 15), "month": (30, 60)}


def series(slots: list[dict[str, Any]], start: str, end: str, bucket_minutes: int = 5) -> dict[str, Any]:
    """Downsample the grid into buckets for charting: means for levels, 'any' for presence/TV, mode for intent/state."""
    rows = [s for s in slots if start <= s["slot"] < end]
    per = max(1, bucket_minutes // SLOT_MINUTES)
    roles = sorted({r for s in rows for r in (s.get("lights") or {})})
    zones = sorted({z for s in rows for z in (s.get("zones") or [])})
    out: dict[str, Any] = {"t": [], "bucket_minutes": bucket_minutes, "lux_in": [], "lux_out": [], "sun_elev": [],
                           "light_max": [], "lights": {r: [] for r in roles}, "occupied": [], "media": [],
                           "zones": {z: [] for z in zones}, "intent": [], "state": []}

    def mean(vals: list) -> float | None:
        v = [x for x in vals if x is not None]
        return round(sum(v) / len(v), 1) if v else None

    def anyv(vals: list) -> int | None:
        v = [x for x in vals if x is not None]
        return (1 if any(v) else 0) if v else None

    for i in range(0, len(rows), per):
        b = rows[i:i + per]
        out["t"].append(b[0]["slot"])
        for k in ("lux_in", "lux_out", "sun_elev", "light_max"):
            out[k].append(mean([s.get(k) for s in b]))
        for r in roles:
            out["lights"][r].append(mean([(s.get("lights") or {}).get(r) for s in b]))
        out["occupied"].append(anyv([s.get("occupied") for s in b]))
        out["media"].append(anyv([s.get("media") for s in b]))
        for z in zones:
            out["zones"][z].append(1 if any(z in (s.get("zones") or []) for s in b) else 0)
        labs = Counter(s.get("intent") for s in b if s.get("intent"))
        out["intent"].append(labs.most_common(1)[0][0] if labs else None)
        sts = Counter(x.get("state") for x in b if x.get("state") is not None)
        out["state"].append(sts.most_common(1)[0][0] if sts else None)
    return out
