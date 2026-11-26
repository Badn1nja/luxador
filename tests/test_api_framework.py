"""API basics: honest when Home Assistant isn't connected, and dead endpoints stay dead."""

from pathlib import Path

from fastapi.testclient import TestClient

from lantern import main as lux_main

CFG = Path(__file__).resolve().parent.parent / "lantern" / "src" / "config"


def _client(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    (cfg / "rooms").mkdir(parents=True)
    for f in ("settings.yaml", "schemas.yaml", "rooms/lounge.yaml"):
        (cfg / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg)
    monkeypatch.setenv("LANTERN_DB_PATH", str(tmp_path / "lantern.db"))
    return TestClient(lux_main.app)


def test_not_connected_shows_nothing_made_up(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        ov = client.get("/api/overview").json()
        room = ov["rooms"][0]
        assert room["room"] == "lounge" and ov["ha_connected"] is False
        assert room["ambient_lux"] == 0 and room["occupied"] is False  # no 439-lux snapshot
        assert any("Not connected" in w for w in room["warnings"])
        st = client.get("/api/rooms/lounge/state").json()
        assert all(not r["on"] for r in st["evaluation"]["roles"].values())  # nothing known, nothing on
        assert "traits" not in st["room_config"] and "tweaks" not in st["room_config"]


def test_removed_endpoints_are_gone(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        for path in ("/api/rooms/lounge/tweaks", "/api/rooms/lounge/simulate-day", "/api/luxie/lounge/rollup",
                     "/api/rooms/lounge/rules/add", "/api/datalab/lounge/labels"):
            assert client.post(path, json={}).status_code in (404, 405), path
        assert client.get("/api/learn/lounge/weekly").status_code == 404
