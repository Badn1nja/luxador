"""Tests for Luxie SQLite telemetry, causal logs, EWMA baseline learning, and lux drift."""

from datetime import datetime, timedelta, timezone

from luxador.luxie import LuxieStore


def test_causal_log_and_bounded_ewma_learning(tmp_path):
    store = LuxieStore(tmp_path / "luxie.db")
    try:
        store.log_causal_event("lounge", "shoelight", 30, 70, "user_keiran", "Manual boost via remote")
        logs = store.get_causal_logs("lounge")
        assert len(logs) == 1
        assert logs[0]["to_bri"] == 70

        # First 2 pushes record overrides without shifting baseline yet
        r1 = store.record_user_punch("lounge", "shoelight", "evening", 40.0)
        r2 = store.record_user_punch("lounge", "shoelight", "evening", 40.0)
        assert r1["offset_pct"] == 0.0 and r2["offset_pct"] == 0.0

        # 3rd consistent push begins nudging baseline safely
        r3 = store.record_user_punch("lounge", "shoelight", "evening", 40.0)
        assert r3["learned"] is True
        assert r3["offset_pct"] > 0.0

        store.reset_offset("lounge", "shoelight", "evening")
        assert store.get_learned_offsets("lounge")["shoelight:evening"] == 0.0
    finally:
        store.close()


def test_rollup_and_lux_drift_detection(tmp_path):
    store = LuxieStore(tmp_path / "luxie.db")
    try:
        base_dt = datetime(2026, 11, 26, 13, 0, tzinfo=timezone.utc)
        for i in range(6):
            store.record_raw_tick("lounge", 450.0 + i, True, "off", "Relaxing", ts=base_dt + timedelta(minutes=i))
        # Inject one wild spike that median/trim should ignore
        store.record_raw_tick("lounge", 9999.0, True, "off", "Relaxing", ts=base_dt + timedelta(minutes=7))

        stats = store.run_rollup(retention_days=7)
        assert stats["hourly_buckets"] == 1
        assert stats["yearly_cells"] == 1

        healthy = store.check_lux_drift("lounge", 440.0, base_dt)
        assert healthy["alert"] is False

        drift = store.check_lux_drift("lounge", 10.0, base_dt)
        assert drift["alert"] is True
        assert drift["status"] == "out_of_sync"
    finally:
        store.close()
