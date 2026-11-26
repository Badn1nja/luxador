"""Luxie store: the causal audit log and that removed tables stay removed."""

from lantern.echo import EchoStore


def test_causal_log(tmp_path):
    store = EchoStore(tmp_path / "lantern.db")
    try:
        store.log_causal_event("lounge", "shoelight", 30, 70, "ha_user", "Manual change via remote")
        logs = store.get_causal_logs("lounge")
        assert len(logs) == 1 and logs[0]["to_bri"] == 70
    finally:
        store.close()


def test_dead_tables_are_dropped(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE raw_events (id INTEGER)")
    con.execute("CREATE TABLE baseline_offsets (id INTEGER)")
    con.commit()
    con.close()
    store = EchoStore(db)
    names = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "raw_events" not in names and "baseline_offsets" not in names and "slots" in names
    store.close()
