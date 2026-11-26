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


class LuxieStore:
    """SQLite-backed intelligence & telemetry sidekick for El Luxador."""

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
            CREATE TABLE IF NOT EXISTS raw_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                room TEXT NOT NULL,
                ambient_lux REAL,
                occupied INTEGER,
                media_state TEXT,
                inferred_state TEXT,
                payload_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_raw_room_ts ON raw_events(room, ts);

            CREATE TABLE IF NOT EXISTS clean_hourly (
                room TEXT NOT NULL,
                hour_bucket TEXT NOT NULL,
                week_of_year INTEGER NOT NULL,
                hour_of_day INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                median_lux REAL NOT NULL,
                p10_lux REAL NOT NULL,
                p90_lux REAL NOT NULL,
                PRIMARY KEY (room, hour_bucket)
            );

            CREATE TABLE IF NOT EXISTS yearly_baselines (
                room TEXT NOT NULL,
                week_of_year INTEGER NOT NULL,
                hour_of_day INTEGER NOT NULL,
                sample_weeks INTEGER NOT NULL,
                expected_lux REAL NOT NULL,
                p10_lux REAL NOT NULL,
                p90_lux REAL NOT NULL,
                PRIMARY KEY (room, week_of_year, hour_of_day)
            );

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

            CREATE TABLE IF NOT EXISTS baseline_offsets (
                room TEXT NOT NULL,
                role TEXT NOT NULL,
                bucket_key TEXT NOT NULL,
                offset_pct REAL NOT NULL DEFAULT 0.0,
                override_count INTEGER NOT NULL DEFAULT 0,
                frozen INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (room, role, bucket_key)
            );

            CREATE TABLE IF NOT EXISTS bulb_lux_gain (
                room TEXT NOT NULL,
                role TEXT NOT NULL,
                gain_lux REAL NOT NULL,
                observations INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (room, role)
            );

            CREATE TABLE IF NOT EXISTS state_confirmations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                room TEXT NOT NULL,
                suggested_state TEXT NOT NULL,
                user_label TEXT NOT NULL,
                confidence REAL NOT NULL,
                snapshot_json TEXT
            );

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

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                signature TEXT NOT NULL,
                start_ts TEXT NOT NULL,
                end_ts TEXT NOT NULL,
                duration_s REAL NOT NULL,
                start_hour INTEGER NOT NULL,
                weekday INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ep_room_sig ON episodes(room, signature);

            CREATE TABLE IF NOT EXISTS pattern_labels (
                room TEXT NOT NULL,
                signature TEXT NOT NULL,
                label TEXT,
                ignored INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (room, signature)
            );

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

            -- What each role's lights ACTUALLY were (not what we asked for), tagged with the presence pattern.
            -- Phase 3 learns lighting levels from a rolling week of this (empty YAML -> learned curves).
            CREATE TABLE IF NOT EXISTS light_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                room TEXT NOT NULL,
                role TEXT NOT NULL,
                signature TEXT NOT NULL,
                state_label TEXT,
                hour_of_day INTEGER NOT NULL,
                weekday INTEGER NOT NULL,
                actual_on INTEGER NOT NULL,
                actual_bri INTEGER,
                actual_kelvin INTEGER,
                actual_rgb TEXT,
                target_bri INTEGER,
                ambient_lux REAL,
                manual INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ls_room_ts ON light_samples(room, ts);

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
            -- Your ground-truth labels: [start, end) local time ranges
            CREATE TABLE IF NOT EXISTS intent_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                intent TEXT NOT NULL,
                created_at TEXT NOT NULL
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

    def record_raw_tick(
        self,
        room: str,
        ambient_lux: float,
        occupied: bool,
        media_state: str,
        inferred_state: str,
        payload: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        """Append a telemetry snapshot to the hot raw_events table."""
        self._conn.execute(
            """
            INSERT INTO raw_events (ts, room, ambient_lux, occupied, media_state, inferred_state, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_iso(ts),
                room,
                float(ambient_lux),
                1 if occupied else 0,
                media_state,
                inferred_state,
                json.dumps(payload or {}),
            ),
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

    def get_learned_offsets(self, room: str) -> dict[str, float]:
        """Return `{ 'role:bucket': offset_pct }` for the room resolver."""
        rows = self._conn.execute(
            "SELECT role, bucket_key, offset_pct FROM baseline_offsets WHERE room = ?",
            (room,),
        ).fetchall()
        return {f"{r['role']}:{r['bucket_key']}": float(r["offset_pct"]) for r in rows}

    def list_baseline_offsets(self, room: str | None = None) -> list[dict[str, Any]]:
        """List all baseline offsets with counts & freeze state."""
        if room:
            rows = self._conn.execute(
                "SELECT * FROM baseline_offsets WHERE room = ? ORDER BY role, bucket_key",
                (room,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM baseline_offsets ORDER BY room, role, bucket_key"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_user_punch(
        self,
        room: str,
        role: str,
        bucket_key: str,
        delta_pct: float,
        alpha: float = 0.15,
        min_overrides_before_learn: int = 3,
    ) -> dict[str, Any]:
        """Track a user manual override and safely nudge learned baseline after consistent pushes."""
        row = self._conn.execute(
            "SELECT * FROM baseline_offsets WHERE room = ? AND role = ? AND bucket_key = ?",
            (room, role, bucket_key),
        ).fetchone()

        now_s = _utc_iso()
        if row is None:
            new_count = 1
            new_offset = 0.0
            frozen = 0
            self._conn.execute(
                """
                INSERT INTO baseline_offsets (room, role, bucket_key, offset_pct, override_count, frozen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (room, role, bucket_key, new_offset, new_count, frozen, now_s),
            )
        else:
            new_count = int(row["override_count"]) + 1
            frozen = int(row["frozen"])
            curr_offset = float(row["offset_pct"])
            if frozen or new_count < min_overrides_before_learn:
                new_offset = curr_offset
            else:
                # Bounded EWMA so one-off pushes cannot run away
                step = max(-10.0, min(10.0, delta_pct * alpha))
                new_offset = round(max(-20.0, min(25.0, curr_offset + step)), 2)

            self._conn.execute(
                """
                UPDATE baseline_offsets
                SET offset_pct = ?, override_count = ?, updated_at = ?
                WHERE room = ? AND role = ? AND bucket_key = ?
                """,
                (new_offset, new_count, now_s, room, role, bucket_key),
            )
        self._conn.commit()
        return {
            "room": room,
            "role": role,
            "bucket_key": bucket_key,
            "override_count": new_count,
            "offset_pct": new_offset,
            "learned": (not frozen and new_count >= min_overrides_before_learn),
        }

    def set_offset_frozen(self, room: str, role: str, bucket_key: str, frozen: bool) -> None:
        self._conn.execute(
            "UPDATE baseline_offsets SET frozen = ?, updated_at = ? WHERE room = ? AND role = ? AND bucket_key = ?",
            (1 if frozen else 0, _utc_iso(), room, role, bucket_key),
        )
        self._conn.commit()

    def reset_offset(self, room: str, role: str, bucket_key: str) -> None:
        self._conn.execute(
            "UPDATE baseline_offsets SET offset_pct = 0.0, override_count = 0, updated_at = ? WHERE room = ? AND role = ? AND bucket_key = ?",
            (_utc_iso(), room, role, bucket_key),
        )
        self._conn.commit()

    def update_bulb_lux_gain(self, room: str, role: str, observed_gain_lux: float) -> float:
        """Update empirical lux contribution (at 100% brightness) via EWMA."""
        clamped = max(2.0, min(500.0, float(observed_gain_lux)))
        row = self._conn.execute(
            "SELECT gain_lux, observations FROM bulb_lux_gain WHERE room = ? AND role = ?",
            (room, role),
        ).fetchone()
        if row is None:
            new_gain, obs = round(clamped, 2), 1
            self._conn.execute(
                "INSERT INTO bulb_lux_gain (room, role, gain_lux, observations, updated_at) VALUES (?, ?, ?, ?, ?)",
                (room, role, new_gain, obs, _utc_iso()),
            )
        else:
            obs = int(row["observations"]) + 1
            new_gain = round(float(row["gain_lux"]) * 0.8 + clamped * 0.2, 2)
            self._conn.execute(
                "UPDATE bulb_lux_gain SET gain_lux = ?, observations = ?, updated_at = ? WHERE room = ? AND role = ?",
                (new_gain, obs, _utc_iso(), room, role),
            )
        self._conn.commit()
        return new_gain

    def get_bulb_lux_gains(self, room: str) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT role, gain_lux FROM bulb_lux_gain WHERE room = ?", (room,)
        ).fetchall()
        return {r["role"]: float(r["gain_lux"]) for r in rows}

    def record_confirmation(
        self,
        room: str,
        suggested_state: str,
        user_label: str,
        confidence: float,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Persist human-in-the-loop state confirmation for active learning."""
        self._conn.execute(
            """
            INSERT INTO state_confirmations (ts, room, suggested_state, user_label, confidence, snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (_utc_iso(), room, suggested_state, user_label, confidence, json.dumps(snapshot or {})),
        )
        self._conn.commit()

    def get_confirmations(self, room: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        q = "SELECT * FROM state_confirmations"
        args: tuple[Any, ...] = (limit,)
        if room:
            q += " WHERE room = ?"
            args = (room, limit)
        q += " ORDER BY id DESC LIMIT ?"
        rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def run_rollup(self, retention_days: int = 7) -> dict[str, int]:
        """Clean raw_events into hourly medians, collate into yearly_baselines, and prune stale raw rows."""
        rows = self._conn.execute(
            "SELECT ts, room, ambient_lux FROM raw_events WHERE ambient_lux IS NOT NULL"
        ).fetchall()

        buckets: dict[tuple[str, str, int, int], list[float]] = {}
        for r in rows:
            dt = datetime.fromisoformat(r["ts"])
            hour_bucket = dt.strftime("%Y-%m-%dT%H")
            week = int(dt.isocalendar().week)
            key = (r["room"], hour_bucket, week, dt.hour)
            buckets.setdefault(key, []).append(float(r["ambient_lux"]))

        hourly_written = 0
        for (room, hbucket, week, hour), vals in buckets.items():
            # Strip extreme single-sample spikes using IQR-like trimming if enough samples
            svals = sorted(vals)
            if len(svals) >= 5:
                trim = max(1, len(svals) // 10)
                svals = svals[trim:-trim] or svals
            n = len(svals)
            med = svals[n // 2]
            p10 = svals[max(0, int(0.1 * (n - 1)))]
            p90 = svals[min(n - 1, int(0.9 * (n - 1)))]
            self._conn.execute(
                """
                INSERT INTO clean_hourly (room, hour_bucket, week_of_year, hour_of_day, sample_count, median_lux, p10_lux, p90_lux)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room, hour_bucket) DO UPDATE SET
                    sample_count = excluded.sample_count,
                    median_lux = excluded.median_lux,
                    p10_lux = excluded.p10_lux,
                    p90_lux = excluded.p90_lux
                """,
                (room, hbucket, week, hour, n, round(med, 2), round(p10, 2), round(p90, 2)),
            )
            hourly_written += 1

        # Collate clean_hourly into yearly_baselines per (room, week_of_year, hour_of_day)
        hrows = self._conn.execute(
            "SELECT room, week_of_year, hour_of_day, median_lux, p10_lux, p90_lux FROM clean_hourly"
        ).fetchall()
        y_groups: dict[tuple[str, int, int], list[tuple[float, float, float]]] = {}
        for hr in hrows:
            y_groups.setdefault(
                (hr["room"], int(hr["week_of_year"]), int(hr["hour_of_day"])), []
            ).append((float(hr["median_lux"]), float(hr["p10_lux"]), float(hr["p90_lux"])))

        yearly_written = 0
        for (room, week, hour), tuples in y_groups.items():
            cnt = len(tuples)
            exp = sum(t[0] for t in tuples) / cnt
            p10 = sum(t[1] for t in tuples) / cnt
            p90 = sum(t[2] for t in tuples) / cnt
            self._conn.execute(
                """
                INSERT INTO yearly_baselines (room, week_of_year, hour_of_day, sample_weeks, expected_lux, p10_lux, p90_lux)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room, week_of_year, hour_of_day) DO UPDATE SET
                    sample_weeks = excluded.sample_weeks,
                    expected_lux = excluded.expected_lux,
                    p10_lux = excluded.p10_lux,
                    p90_lux = excluded.p90_lux
                """,
                (room, week, hour, cnt, round(exp, 2), round(p10, 2), round(p90, 2)),
            )
            yearly_written += 1

        cutoff = _utc_iso(datetime.now(timezone.utc) - timedelta(days=retention_days))
        cur = self._conn.execute("DELETE FROM raw_events WHERE ts < ?", (cutoff,))
        self._conn.commit()
        return {
            "hourly_buckets": hourly_written,
            "yearly_cells": yearly_written,
            "pruned_raw_rows": cur.rowcount,
        }

    def check_lux_drift(self, room: str, current_lux: float, now: datetime) -> dict[str, Any]:
        """Compare live lux against yearly baseline (or nocturnal sanity check) to flag sensor drift."""
        week = int(now.isocalendar().week)
        row = self._conn.execute(
            "SELECT * FROM yearly_baselines WHERE room = ? AND week_of_year = ? AND hour_of_day = ?",
            (room, week, now.hour),
        ).fetchone()

        if row is not None and int(row["sample_weeks"]) >= 1:
            exp = float(row["expected_lux"])
            low = float(row["p10_lux"]) * 0.4
            high = max(float(row["p90_lux"]) * 2.0, exp + 150.0)
            drift = current_lux < low or current_lux > high
            return {
                "alert": drift,
                "status": "out_of_sync" if drift else "healthy",
                "current_lux": round(current_lux, 1),
                "expected_lux": round(exp, 1),
                "range": [round(low, 1), round(high, 1)],
                "message": (
                    f"Lux {current_lux:.0f} is out of sync with seasonal baseline ({exp:.0f} lux)"
                    if drift
                    else f"Tracking baseline ({exp:.0f} lux)"
                ),
            }

        # Fallback sanity checks before 1+ weeks of data accumulate
        is_night = now.hour < 5 or now.hour >= 23
        if is_night and current_lux > 250:
            return {
                "alert": True,
                "status": "suspect_high_night",
                "current_lux": round(current_lux, 1),
                "expected_lux": 10.0,
                "message": f"Lux reading {current_lux:.0f} at night looks stuck/uncalibrated",
            }
        return {
            "alert": False,
            "status": "learning_baseline",
            "current_lux": round(current_lux, 1),
            "expected_lux": None,
            "message": "Collecting clean baseline samples (no drift detected)",
        }

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

    def record_episode(self, room: str, signature: str, start: datetime, end: datetime) -> None:
        dur = max(0.0, (end - start).total_seconds())
        if dur <= 0:
            return
        self._conn.execute(
            "INSERT INTO episodes (room, signature, start_ts, end_ts, duration_s, start_hour, weekday) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (room, signature, _utc_iso(start), _utc_iso(end), dur, start.hour, start.weekday()),
        )
        self._conn.commit()

    def get_pattern_labels(self, room: str) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT signature, label, ignored FROM pattern_labels WHERE room = ?", (room,)
        ).fetchall()
        return {r["signature"]: {"label": r["label"], "ignored": bool(r["ignored"])} for r in rows}

    def set_pattern_label(self, room: str, signature: str, label: str | None, ignored: bool = False) -> None:
        clean = (label or "").strip().lower() or None
        if clean is None and not ignored:
            self._conn.execute("DELETE FROM pattern_labels WHERE room = ? AND signature = ?", (room, signature))
        else:
            self._conn.execute(
                """
                INSERT INTO pattern_labels (room, signature, label, ignored, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(room, signature) DO UPDATE SET label = excluded.label, ignored = excluded.ignored,
                    updated_at = excluded.updated_at
                """,
                (room, signature, clean, 1 if ignored else 0, _utc_iso()),
            )
        self._conn.commit()

    def resolve_state(self, room: str, signature: str) -> tuple[str | None, str | None]:
        """Name for a signature: exact label, else the closest labelled pattern (Jaccard >= 0.5).

        Returns (label, 'exact'|'approx') or (None, None). Empty signature is always 'empty'.
        """
        if signature == "":
            return "empty", "exact"
        labels = {s: v["label"] for s, v in self.get_pattern_labels(room).items() if v["label"] and not v["ignored"]}
        if signature in labels:
            return labels[signature], "exact"
        toks = set(signature.split("+"))
        best, best_score = None, 0.0
        for sig, lab in labels.items():
            other = set(sig.split("+")) if sig else set()
            if not other:
                continue
            score = len(toks & other) / len(toks | other)
            if score > best_score:
                best, best_score = lab, score
        if best is not None and best_score >= 0.5:
            return best, "approx"
        return None, None

    def pattern_summary(
        self, room: str, min_duration_s: float = 120.0, propose_min_count: int = 3,
        propose_min_minutes: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Every signature seen (ignoring blips), with how often/long/when, label, and whether to ask about it."""
        rows = self._conn.execute(
            "SELECT signature, duration_s, start_hour, end_ts FROM episodes WHERE room = ? AND duration_s >= ?",
            (room, min_duration_s),
        ).fetchall()
        labels = self.get_pattern_labels(room)
        groups: dict[str, dict[str, Any]] = {}
        for r in rows:
            g = groups.setdefault(r["signature"], {"count": 0, "total_s": 0.0, "hours": [0] * 24, "last_seen": ""})
            g["count"] += 1
            g["total_s"] += float(r["duration_s"])
            g["hours"][int(r["start_hour"])] += 1
            g["last_seen"] = max(g["last_seen"], r["end_ts"])
        out = []
        for sig, g in groups.items():
            lab = labels.get(sig, {})
            top_hours = sorted(range(24), key=lambda h: -g["hours"][h])[:3]
            total_min = g["total_s"] / 60.0
            labelled = bool(lab.get("label")) or sig == ""
            out.append({
                "signature": sig,
                "tokens": sig.split("+") if sig else [],
                "label": "empty" if sig == "" else lab.get("label"),
                "ignored": bool(lab.get("ignored")),
                "count": g["count"],
                "total_minutes": round(total_min, 1),
                "avg_minutes": round(total_min / g["count"], 1),
                "typical_hours": [h for h in top_hours if g["hours"][h] > 0],
                "last_seen": g["last_seen"],
                "propose": (not labelled and not lab.get("ignored")
                            and (g["count"] >= propose_min_count or total_min >= propose_min_minutes)),
            })
        out.sort(key=lambda p: (-int(p["propose"]), -p["total_minutes"]))
        return out

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
        """Lights something other than Luxador changed recently, grouped by entity and who did it."""
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

    def record_light_sample(
        self, room: str, role: str, signature: str, state_label: str | None, actual_on: bool,
        actual_bri: int | None, actual_kelvin: int | None, actual_rgb: Any, target_bri: int | None,
        ambient_lux: float | None, manual: bool, ts: datetime | None = None,
    ) -> None:
        ts = ts or datetime.now()
        self._conn.execute(
            """
            INSERT INTO light_samples (ts, room, role, signature, state_label, hour_of_day, weekday, actual_on,
                actual_bri, actual_kelvin, actual_rgb, target_bri, ambient_lux, manual)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_utc_iso(ts), room, role, signature, state_label, ts.hour, ts.weekday(), 1 if actual_on else 0,
             actual_bri, actual_kelvin, json.dumps(actual_rgb) if actual_rgb else None, target_bri,
             ambient_lux, 1 if manual else 0),
        )
        self._conn.commit()

    def weekly_light_profile(self, room: str, days: int = 7) -> dict[str, Any]:
        """Phase 3 preview: per (state, role, hour) median of what lights actually were over the last `days`.

        Read-only. Nothing drives the bulbs from this yet; it shows what a learned curve would look like.
        """
        cutoff = _utc_iso(datetime.now(timezone.utc) - timedelta(days=days))
        rows = self._conn.execute(
            """SELECT role, COALESCE(state_label, signature) AS st, hour_of_day, actual_on, actual_bri, actual_kelvin
               FROM light_samples WHERE room = ? AND ts >= ?""",
            (room, cutoff),
        ).fetchall()
        cells: dict[tuple[str, str, int], dict[str, list]] = {}
        for r in rows:
            c = cells.setdefault((r["st"] or "empty", r["role"], int(r["hour_of_day"])), {"bri": [], "k": [], "on": []})
            c["on"].append(int(r["actual_on"]))
            c["bri"].append(int(r["actual_bri"] or 0) if r["actual_on"] else 0)
            if r["actual_kelvin"]:
                c["k"].append(int(r["actual_kelvin"]))

        def med(v: list[int]) -> int | None:
            return sorted(v)[len(v) // 2] if v else None

        out: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for (st, role, hour), c in sorted(cells.items()):
            out.setdefault(st, {}).setdefault(role, []).append({
                "hour": hour,
                "samples": len(c["on"]),
                "on_ratio": round(sum(c["on"]) / len(c["on"]), 2),
                "median_bri": med(c["bri"]),
                "median_kelvin": med(c["k"]),
            })
        return {"days": days, "sample_rows": len(rows), "profile": out}

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

    def add_intent_label(self, room: str, start: str, end: str, intent: str) -> None:
        self._conn.execute(
            "INSERT INTO intent_labels (room, start, end, intent, created_at) VALUES (?, ?, ?, ?, ?)",
            (room, start, end, intent, _utc_iso()),
        )
        self._conn.commit()

    def get_intent_labels(self, room: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM intent_labels WHERE room = ? ORDER BY start", (room,)).fetchall()]

    def delete_intent_label(self, room: str, label_id: int) -> None:
        self._conn.execute("DELETE FROM intent_labels WHERE room = ? AND id = ?", (room, label_id))
        self._conn.commit()

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

    def get_telemetry_summary(self, room: str) -> dict[str, Any]:
        raw_count = self._conn.execute("SELECT COUNT(*) FROM raw_events WHERE room = ?", (room,)).fetchone()[0]
        hourly_count = self._conn.execute("SELECT COUNT(*) FROM clean_hourly WHERE room = ?", (room,)).fetchone()[0]
        yearly_count = self._conn.execute("SELECT COUNT(*) FROM yearly_baselines WHERE room = ?", (room,)).fetchone()[0]
        yearly_rows = [
            dict(r)
            for r in self._conn.execute(
                "SELECT week_of_year, hour_of_day, expected_lux, p10_lux, p90_lux FROM yearly_baselines WHERE room = ? ORDER BY week_of_year, hour_of_day LIMIT 48",
                (room,),
            ).fetchall()
        ]
        return {
            "counts": {
                "raw_events": raw_count,
                "clean_hourly": hourly_count,
                "yearly_baselines": yearly_count,
                "signal_events": self._conn.execute("SELECT COUNT(*) FROM signal_events WHERE room = ?", (room,)).fetchone()[0],
                "episodes": self._conn.execute("SELECT COUNT(*) FROM episodes WHERE room = ?", (room,)).fetchone()[0],
                "light_samples": self._conn.execute("SELECT COUNT(*) FROM light_samples WHERE room = ?", (room,)).fetchone()[0],
            },
            "yearly_preview": yearly_rows,
            "lux_gains": self.get_bulb_lux_gains(room),
        }
