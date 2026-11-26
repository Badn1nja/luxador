"""Luxie — Telemetry, SQLite 3-tier rollups, causal ledger, lux drift checks, and safe EWMA learning."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utc_iso(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class EchoStore:
    """Echo: Lantern's memory. Everything the house did, in SQLite (history, the 5-minute grid, logs, links)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            -- Removed in v0.7 (fake, unused, or duplicates of the 5-minute grid)
            DROP TABLE IF EXISTS raw_events;
            DROP TABLE IF EXISTS clean_hourly;
            DROP TABLE IF EXISTS yearly_baselines;
            DROP TABLE IF EXISTS baseline_offsets;
            DROP TABLE IF EXISTS bulb_lux_gain;
            DROP TABLE IF EXISTS state_confirmations;
            DROP TABLE IF EXISTS episodes;
            DROP TABLE IF EXISTS pattern_labels;
            DROP TABLE IF EXISTS light_samples;
            DROP TABLE IF EXISTS intent_labels;



            CREATE TABLE IF NOT EXISTS causal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                room TEXT NOT NULL,
                role TEXT NOT NULL,
                entity_id TEXT,
                from_bri INTEGER,
                to_bri INTEGER,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                breakdown_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_causal_room_ts ON causal_log(room, ts);




            -- Presence 'truth' tables: only fed from live HA/OPNsense, never from the sandbox
            CREATE TABLE IF NOT EXISTS signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                room TEXT NOT NULL,
                scope TEXT NOT NULL,
                signal TEXT NOT NULL,
                source TEXT NOT NULL,
                active INTEGER NOT NULL,
                available INTEGER NOT NULL,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sig_room_ts ON signal_events(room, ts);



            CREATE TABLE IF NOT EXISTS foreign_writes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                room TEXT,
                entity_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                actor TEXT,
                from_bri INTEGER,
                to_bri INTEGER,
                context_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fw_entity_ts ON foreign_writes(entity_id, ts);


            -- Minute grid for correlation: was each zone (or 'room') occupied this minute?
            CREATE TABLE IF NOT EXISTS presence_minutes (
                minute TEXT NOT NULL,
                room TEXT NOT NULL,
                zone TEXT NOT NULL,
                occupied INTEGER NOT NULL,
                PRIMARY KEY (minute, room, zone)
            );
            -- Peak traffic per LAN host per minute (only hosts above a chatter floor are stored)
            CREATE TABLE IF NOT EXISTS net_minutes (
                minute TEXT NOT NULL,
                host TEXT NOT NULL,
                hostname TEXT,
                kbps REAL NOT NULL,
                PRIMARY KEY (minute, host)
            );
            -- Phase 1 traceback: raw HA state history (imported from the recorder, then captured live)
            CREATE TABLE IF NOT EXISTS ha_history (
                entity_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                state TEXT NOT NULL,
                attrs_json TEXT,
                source TEXT NOT NULL DEFAULT 'import',
                PRIMARY KEY (entity_id, ts)
            );
            -- HA long-term statistics (hourly) for numeric sensors, e.g. indoor/outdoor lux
            CREATE TABLE IF NOT EXISTS lts_hourly (
                statistic_id TEXT NOT NULL,
                hour_ts TEXT NOT NULL,
                mean REAL, min REAL, max REAL,
                PRIMARY KEY (statistic_id, hour_ts)
            );
            -- The 5-minute grid (rebuilt from ha_history) with intent labels
            CREATE TABLE IF NOT EXISTS slots (
                room TEXT NOT NULL,
                slot TEXT NOT NULL,
                data_json TEXT NOT NULL,
                intent TEXT,
                intent_source TEXT,
                PRIMARY KEY (room, slot)
            );
            -- Forever, but tiny: every grid slot that ages out of the detail window is folded in here.
            -- One row per (room, ISO week, hour, daytype): ~2.5k rows per room, ever.
            CREATE TABLE IF NOT EXISTS seasonal (
                room TEXT NOT NULL,
                week INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                daytype TEXT NOT NULL,
                n INTEGER NOT NULL DEFAULT 0,
                occ_n INTEGER NOT NULL DEFAULT 0,
                occ_known INTEGER NOT NULL DEFAULT 0,
                light_on INTEGER NOT NULL DEFAULT 0,
                light_bri_sum REAL NOT NULL DEFAULT 0,
                lux_in_sum REAL NOT NULL DEFAULT 0,
                lux_in_n INTEGER NOT NULL DEFAULT 0,
                sun_sum REAL NOT NULL DEFAULT 0,
                sun_n INTEGER NOT NULL DEFAULT 0,
                roles_json TEXT NOT NULL DEFAULT '{}',
                intents_json TEXT NOT NULL DEFAULT '{}',
                years_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (room, week, hour, daytype)
            );

            CREATE TABLE IF NOT EXISTS model_cache (
                room TEXT NOT NULL,
                kind TEXT NOT NULL,
                built_at TEXT NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY (room, kind)
            );

            -- Device <-> zone links you accepted (or rejected) from the learned proposals
            CREATE TABLE IF NOT EXISTS learned_signals (
                room TEXT NOT NULL,
                host TEXT NOT NULL,
                zone TEXT,
                name TEXT,
                active_kbps REAL,
                status TEXT NOT NULL,  -- accepted | ignored
                updated_at TEXT NOT NULL,
                PRIMARY KEY (room, host)
            );
            """
        )
        self._conn.commit()

    def log_causal_event(
        self,
        room: str,
        role: str,
        from_bri: int,
        to_bri: int,
        actor: str,
        reason: str,
        entity_id: str | None = None,
        breakdown: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        """Log a human-readable causal explanation ('Why am I at 70%?')."""
        self._conn.execute(
            """
            INSERT INTO causal_log (ts, room, role, entity_id, from_bri, to_bri, actor, reason, breakdown_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_iso(ts),
                room,
                role,
                entity_id,
                int(from_bri),
                int(to_bri),
                actor,
                reason,
                json.dumps(breakdown or {}),
            ),
        )
        self._conn.commit()

    def get_causal_logs(self, room: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
        """Retrieve recent causal log entries."""
        if room:
            rows = self._conn.execute(
                "SELECT * FROM causal_log WHERE room = ? ORDER BY id DESC LIMIT ?",
                (room, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM causal_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["breakdown"] = json.loads(d.pop("breakdown_json") or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------------ presence truth

    def record_signal_event(
        self, room: str, scope: str, signal: str, source: str, active: bool, available: bool,
        value: Any = None, ts: datetime | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO signal_events (ts, room, scope, signal, source, active, available, value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_utc_iso(ts), room, scope, signal, source, 1 if active else 0, 1 if available else 0,
             None if value is None else str(value)),
        )
        self._conn.commit()

    def record_foreign_write(
        self, entity_id: str, kind: str, actor: str, from_bri: int, to_bri: int,
        room: str | None = None, context_id: str | None = None, ts: datetime | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO foreign_writes (ts, room, entity_id, kind, actor, from_bri, to_bri, context_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_utc_iso(ts), room, entity_id, kind, actor, int(from_bri), int(to_bri), context_id),
        )
        self._conn.commit()

    def contested_lights(self, room: str | None = None, hours: float = 24.0) -> list[dict[str, Any]]:
        """Lights something other than Flint changed recently, grouped by entity and who did it."""
        cutoff = _utc_iso(datetime.now(timezone.utc) - timedelta(hours=hours))
        q = "SELECT entity_id, kind, COUNT(*) AS n, MAX(ts) AS last_ts FROM foreign_writes WHERE ts >= ?"
        args: list[Any] = [cutoff]
        if room:
            q += " AND room = ?"
            args.append(room)
        q += " GROUP BY entity_id, kind ORDER BY n DESC"
        out: dict[str, dict[str, Any]] = {}
        for r in self._conn.execute(q, args).fetchall():
            e = out.setdefault(r["entity_id"], {"entity_id": r["entity_id"], "by_kind": {}, "total": 0, "last_ts": ""})
            e["by_kind"][r["kind"]] = r["n"]
            e["total"] += r["n"]
            e["last_ts"] = max(e["last_ts"], r["last_ts"])
        for e in out.values():
            # A user nudging a light is fine; an automation/integration writing to it means two controllers.
            e["fighting"] = e["by_kind"].get("automation", 0) + e["by_kind"].get("integration", 0) >= 2
        return sorted(out.values(), key=lambda e: -e["total"])

    # ------------------------------------------------------------------ Phase 1 traceback storage

    def store_history(self, rows: list[tuple[str, str, str, str]], source: str = "import") -> int:
        cur = self._conn.executemany(
            "INSERT OR IGNORE INTO ha_history (entity_id, ts, state, attrs_json, source) VALUES (?, ?, ?, ?, ?)",
            [(*r, source) for r in rows],
        )
        self._conn.commit()
        return cur.rowcount

    def load_history(self, entity_ids: list[str], since_iso: str | None = None) -> dict[str, list[tuple[str, str, str]]]:
        """Per entity: rows from the last state *before* since_iso (so the step function starts right) onward."""
        out: dict[str, list[tuple[str, str, str]]] = {}
        for e in entity_ids:
            rows = []
            if since_iso:
                prev = self._conn.execute(
                    "SELECT ts, state, attrs_json FROM ha_history WHERE entity_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                    (e, since_iso),
                ).fetchone()
                if prev:
                    rows.append(tuple(prev))
                rows += [tuple(r) for r in self._conn.execute(
                    "SELECT ts, state, attrs_json FROM ha_history WHERE entity_id = ? AND ts >= ? ORDER BY ts", (e, since_iso))]
            else:
                rows = [tuple(r) for r in self._conn.execute(
                    "SELECT ts, state, attrs_json FROM ha_history WHERE entity_id = ? ORDER BY ts", (e,))]
            out[e] = rows
        return out

    def history_span(self) -> dict[str, Any]:
        r = self._conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM ha_history").fetchone()
        by_src = {row[0]: row[1] for row in self._conn.execute("SELECT source, COUNT(*) FROM ha_history GROUP BY source")}
        return {"first": r[0], "last": r[1], "rows": r[2], "by_source": by_src,
                "entities": self._conn.execute("SELECT COUNT(DISTINCT entity_id) FROM ha_history").fetchone()[0]}

    def store_statistics(self, rows: list[tuple[str, str, Any, Any, Any]]) -> int:
        cur = self._conn.executemany(
            """INSERT INTO lts_hourly (statistic_id, hour_ts, mean, min, max) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(statistic_id, hour_ts) DO UPDATE SET mean = excluded.mean, min = excluded.min, max = excluded.max""",
            rows,
        )
        self._conn.commit()
        return cur.rowcount

    def load_statistics(self, statistic_ids: list[str]) -> list[dict[str, Any]]:
        if not statistic_ids:
            return []
        q = f"SELECT * FROM lts_hourly WHERE statistic_id IN ({','.join('?' * len(statistic_ids))}) ORDER BY hour_ts"
        return [dict(r) for r in self._conn.execute(q, statistic_ids).fetchall()]

    def replace_slots(self, room: str, slots: list[dict[str, Any]]) -> None:
        self._conn.execute("DELETE FROM slots WHERE room = ?", (room,))
        self._conn.executemany(
            "INSERT INTO slots (room, slot, data_json, intent, intent_source) VALUES (?, ?, ?, ?, ?)",
            [(room, s["slot"], json.dumps({k: v for k, v in s.items() if not k.startswith("_")}),
              s.get("intent"), s.get("intent_source")) for s in slots],
        )
        self._conn.commit()

    def load_slots(self, room: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT data_json FROM slots WHERE room = ? ORDER BY slot", (room,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_model(self, room: str, kind: str, data_json: str) -> None:
        self._conn.execute(
            """INSERT INTO model_cache (room, kind, built_at, data_json) VALUES (?, ?, ?, ?)
               ON CONFLICT(room, kind) DO UPDATE SET built_at = excluded.built_at, data_json = excluded.data_json""",
            (room, kind, _utc_iso(), data_json),
        )
        self._conn.commit()

    def load_model(self, room: str, kind: str) -> tuple[str, str] | None:
        r = self._conn.execute("SELECT built_at, data_json FROM model_cache WHERE room = ? AND kind = ?",
                               (room, kind)).fetchone()
        return (r[0], r[1]) if r else None

    # ------------------------------------------------------------------ device <-> zone learning

    def record_presence_minute(self, minute: str, room: str, zone_occupancy: dict[str, bool]) -> None:
        self._conn.executemany(
            """INSERT INTO presence_minutes (minute, room, zone, occupied) VALUES (?, ?, ?, ?)
               ON CONFLICT(minute, room, zone) DO UPDATE SET occupied = MAX(occupied, excluded.occupied)""",
            [(minute, room, z, 1 if occ else 0) for z, occ in zone_occupancy.items()],
        )
        self._conn.commit()

    def record_net_minute(self, minute: str, hosts: dict[str, tuple[str, float]]) -> None:
        """hosts: host_key(mac) -> (hostname, peak kbps this minute)."""
        self._conn.executemany(
            """INSERT INTO net_minutes (minute, host, hostname, kbps) VALUES (?, ?, ?, ?)
               ON CONFLICT(minute, host) DO UPDATE SET kbps = MAX(kbps, excluded.kbps)""",
            [(minute, h, name, round(k, 1)) for h, (name, k) in hosts.items()],
        )
        self._conn.commit()

    def prune_minutes(self, keep_days: int = 30) -> None:
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M")
        self._conn.execute("DELETE FROM presence_minutes WHERE minute < ?", (cutoff,))
        self._conn.execute("DELETE FROM net_minutes WHERE minute < ?", (cutoff,))
        self._conn.commit()

    def set_learned_signal(self, room: str, host: str, status: str, zone: str | None = None,
                           name: str | None = None, active_kbps: float | None = None) -> None:
        if status == "clear":
            self._conn.execute("DELETE FROM learned_signals WHERE room = ? AND host = ?", (room, host))
        else:
            self._conn.execute(
                """INSERT INTO learned_signals (room, host, zone, name, active_kbps, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(room, host) DO UPDATE SET zone = excluded.zone, name = excluded.name,
                     active_kbps = excluded.active_kbps, status = excluded.status, updated_at = excluded.updated_at""",
                (room, host, zone, name, active_kbps, status, _utc_iso()),
            )
        self._conn.commit()

    def get_learned_signals(self, room: str) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM learned_signals WHERE room = ?", (room,)).fetchall()
        return [dict(r) for r in rows]

    def net_zone_associations(
        self, room: str, days: int = 7, active_kbps: float = 20.0, min_minutes: int = 60,
    ) -> dict[str, Any]:
        """Which LAN devices move with which zones.

        Only minutes where presence was being recorded count. For each host 'active' = peak >= active_kbps.
          p_life  = share of its active minutes where the room had someone in it
          p_zone  = share of its active minutes where a given zone was occupied
          lift    = p_zone / how often that zone is occupied at all
        A device busy while nobody is home (p_life < 0.85) is *background* (NAS, TV updates, cameras): it says
        nothing about people, so it's never proposed. Garbage in, garbage out.
        """
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
        pres: dict[str, dict[str, bool]] = {}
        for r in self._conn.execute(
            "SELECT minute, zone, occupied FROM presence_minutes WHERE room = ? AND minute >= ?", (room, cutoff)
        ).fetchall():
            pres.setdefault(r["minute"], {})[r["zone"]] = bool(r["occupied"])
        total_minutes = len(pres)
        zones = sorted({z for m in pres.values() for z in m if z != "room"})
        base = {z: sum(1 for m in pres.values() if m.get(z)) / total_minutes for z in zones} if total_minutes else {}

        hosts: dict[str, dict[str, Any]] = {}
        for r in self._conn.execute(
            "SELECT minute, host, hostname, kbps FROM net_minutes WHERE minute >= ? AND kbps >= ?", (cutoff, active_kbps)
        ).fetchall():
            if r["minute"] not in pres:
                continue
            h = hosts.setdefault(r["host"], {"hostname": r["hostname"] or "", "minutes": [], "kbps": []})
            h["minutes"].append(r["minute"])
            h["kbps"].append(float(r["kbps"]))

        decided = {s["host"]: s for s in self.get_learned_signals(room)}
        out = []
        for host, h in hosts.items():
            n = len(h["minutes"])
            if n == 0:
                continue
            life = sum(1 for m in h["minutes"] if pres[m].get("room") or any(pres[m].get(z) for z in zones)) / n
            per_zone = []
            for z in zones:
                pz = sum(1 for m in h["minutes"] if pres[m].get(z)) / n
                lift = pz / base[z] if base.get(z) else 0.0
                per_zone.append({"zone": z, "p_zone": round(pz, 2), "lift": round(lift, 2)})
            best = max(per_zone, key=lambda x: (x["p_zone"] * min(x["lift"], 5)), default=None)
            if life < 0.85:
                kind = "background"
            elif best and best["p_zone"] >= 0.7 and best["lift"] >= 1.5:
                kind = "zone"
            else:
                kind = "room"
            kb = sorted(h["kbps"])
            d = decided.get(host)
            out.append({
                "host": host,
                "hostname": h["hostname"],
                "active_minutes": n,
                "enough_data": n >= min_minutes,
                "p_life": round(life, 2),
                "kind": kind,
                "best_zone": best["zone"] if best and kind == "zone" else None,
                "zones": per_zone,
                "median_kbps": round(kb[len(kb) // 2], 1),
                "status": d["status"] if d else None,
                "linked_zone": d["zone"] if d else None,
                "propose": kind == "zone" and n >= min_minutes and d is None,
            })
        out.sort(key=lambda x: (-int(x["propose"]), x["kind"] != "zone", -x["active_minutes"]))
        return {"days": days, "presence_minutes": total_minutes, "zone_base_rates": base, "devices": out}

    def signal_health(self, room: str, hours: float = 24.0) -> dict[str, dict[str, Any]]:
        """Flag signals that would poison learning: flapping, or unavailable much of the time."""
        cutoff = _utc_iso(datetime.now(timezone.utc) - timedelta(hours=hours))
        rows = self._conn.execute(
            "SELECT scope, signal, available, ts FROM signal_events WHERE room = ? AND ts >= ? ORDER BY ts",
            (room, cutoff),
        ).fetchall()
        per: dict[str, dict[str, Any]] = {}
        for r in rows:
            k = f"{r['scope']}.{r['signal']}"
            e = per.setdefault(k, {"changes": 0, "unavailable_events": 0})
            e["changes"] += 1
            e["unavailable_events"] += 0 if r["available"] else 1
        for k, e in per.items():
            rate = e["changes"] / max(1.0, hours)
            e["changes_per_hour"] = round(rate, 1)
            issues = []
            if rate > 20:
                issues.append("flapping: add a delay/filter on the sensor")
            if e["unavailable_events"] >= 5:
                issues.append("drops out often")
            e["issues"] = issues
            e["healthy"] = not issues
        return per

    # ------------------------------------------------------------------ retention: nothing grows forever

    DEFAULT_RETENTION = {
        "causal_log": 14,       # "why am I at X%" audit trail
        "signal_events": 90,
        "foreign_writes": 30,
        "ha_history": 90,       # raw HA states: the detail window the grid is rebuilt from
    }

    def apply_retention(self, policy: dict[str, int] | None = None) -> dict[str, int]:
        """Delete rows older than each table's window. ha_history keeps the last state per entity before the
        cutoff, so the grid's step functions still start from a known state."""
        pol = {**self.DEFAULT_RETENTION, **(policy or {})}
        now = datetime.now(timezone.utc)
        deleted: dict[str, int] = {}
        simple = {"causal_log": "ts", "signal_events": "ts", "foreign_writes": "ts"}
        for table, col in simple.items():
            cutoff = _utc_iso(now - timedelta(days=pol[table]))
            deleted[table] = self._conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
        cutoff = _utc_iso(now - timedelta(days=pol["ha_history"]))
        deleted["ha_history"] = self._conn.execute(
            """DELETE FROM ha_history WHERE ts < :c AND (entity_id, ts) NOT IN
               (SELECT entity_id, MAX(ts) FROM ha_history WHERE ts < :c GROUP BY entity_id)""",
            {"c": cutoff},
        ).rowcount
        self._conn.commit()
        return deleted

    def vacuum(self) -> None:
        """Give freed pages back to the disk (SQLite files don't shrink on DELETE alone)."""
        self._conn.execute("VACUUM")

    def storage_report(self) -> dict[str, Any]:
        tables = [r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        rows = {t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
        free = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
        return {
            "file_mb": round(page_size * pages / 1e6, 2),
            "reclaimable_mb": round(page_size * free / 1e6, 2),
            "rows": dict(sorted(rows.items(), key=lambda kv: -kv[1])),
            "retention_days": self.DEFAULT_RETENTION,
        }

    # ------------------------------------------------------------------ seasonal (yearly) memory

    def merge_seasonal(self, room: str, cells: dict[tuple[int, int, str], dict[str, Any]]) -> None:
        for (week, hour, daytype), c in cells.items():
            row = self._conn.execute(
                "SELECT * FROM seasonal WHERE room = ? AND week = ? AND hour = ? AND daytype = ?",
                (room, week, hour, daytype),
            ).fetchone()
            roles = json.loads(row["roles_json"]) if row else {}
            for r, (on, bri, n) in c["roles"].items():
                o = roles.get(r, [0, 0.0, 0])
                roles[r] = [o[0] + on, round(o[1] + bri, 1), o[2] + n]
            intents = json.loads(row["intents_json"]) if row else {}
            for it, n in c["intents"].items():
                intents[it] = intents.get(it, 0) + n
            years = sorted(set(json.loads(row["years_json"]) if row else []) | set(c["years"]))
            vals = [c[k] + (row[k] if row else 0) for k in
                    ("n", "occ_n", "occ_known", "light_on", "light_bri_sum", "lux_in_sum", "lux_in_n", "sun_sum", "sun_n")]
            self._conn.execute(
                """INSERT INTO seasonal (room, week, hour, daytype, n, occ_n, occ_known, light_on, light_bri_sum,
                       lux_in_sum, lux_in_n, sun_sum, sun_n, roles_json, intents_json, years_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(room, week, hour, daytype) DO UPDATE SET n = excluded.n, occ_n = excluded.occ_n,
                     occ_known = excluded.occ_known, light_on = excluded.light_on, light_bri_sum = excluded.light_bri_sum,
                     lux_in_sum = excluded.lux_in_sum, lux_in_n = excluded.lux_in_n, sun_sum = excluded.sun_sum,
                     sun_n = excluded.sun_n, roles_json = excluded.roles_json, intents_json = excluded.intents_json,
                     years_json = excluded.years_json""",
                (room, week, hour, daytype, *vals, json.dumps(roles), json.dumps(intents), json.dumps(years)),
            )
        self._conn.commit()

    def load_seasonal(self, room: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM seasonal WHERE room = ? ORDER BY week, daytype, hour", (room,)).fetchall()]
