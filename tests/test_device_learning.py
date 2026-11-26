"""Device <-> zone association learning: zone-linked devices get proposed, background chatter is filtered out."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from luxador.luxie import LuxieStore


def _seed(store: LuxieStore, minutes: int = 600) -> None:
    start = datetime.now() - timedelta(minutes=minutes)
    for i in range(minutes):
        m = (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M")
        desk = i % 300 < 120   # at the desk 40% of the time
        sofa = 150 <= i % 300 < 230
        room = desk or sofa
        store.record_presence_minute(m, "lounge", {"room": room, "desk_kwn": desk, "sofa": sofa})
        hosts = {"nas": ("nas.lan", 300.0)}  # busy all the time, even when nobody is home
        if desk:
            hosts["aa:bb:cc:00:11:22"] = ("kwn-laptop.lan", 150.0)
        if room and i % 2 == 0:
            hosts["phone"] = ("pixel", 60.0)  # comes and goes with the person, not a zone
        store.record_net_minute(m, hosts)


def test_laptop_links_to_desk_and_nas_is_background(tmp_path):
    store = LuxieStore(tmp_path / "t.db")
    _seed(store)
    res = store.net_zone_associations("lounge")
    dev = {d["host"]: d for d in res["devices"]}

    lap = dev["aa:bb:cc:00:11:22"]
    assert lap["kind"] == "zone" and lap["best_zone"] == "desk_kwn" and lap["propose"]
    assert dev["nas"]["kind"] == "background" and not dev["nas"]["propose"]
    assert dev["phone"]["kind"] == "room" and not dev["phone"]["propose"]
    assert res["devices"][0]["host"] == "aa:bb:cc:00:11:22"  # proposals first

    store.set_learned_signal("lounge", "aa:bb:cc:00:11:22", "accepted", zone="desk_kwn", name="laptop")
    again = {d["host"]: d for d in store.net_zone_associations("lounge")["devices"]}
    assert again["aa:bb:cc:00:11:22"]["status"] == "accepted" and not again["aa:bb:cc:00:11:22"]["propose"]


def test_accepted_device_becomes_zone_signal(tmp_path, monkeypatch):
    from pathlib import Path

    from luxador import main as lux_main

    cfg_dst = tmp_path / "config"
    (cfg_dst / "rooms").mkdir(parents=True)
    src = Path(__file__).resolve().parent.parent / "luxador" / "src" / "config"
    for f in ("settings.yaml", "schemas.yaml"):
        (cfg_dst / f).write_text((src / f).read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dst / "rooms" / "lounge.yaml").write_text((src / "rooms" / "lounge.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg_dst)
    monkeypatch.setenv("LUXADOR_DB_PATH", str(tmp_path / "luxie.db"))

    with TestClient(lux_main.app) as client:
        bad = client.post("/api/learn/lounge/devices", json={"host": "x", "status": "accepted", "zone": "nope"})
        assert bad.status_code == 400
        ok = client.post("/api/learn/lounge/devices",
                         json={"host": "kwn-laptop.lan", "status": "accepted", "zone": "desk_kwn"})
        assert ok.status_code == 200
        eff = lux_main.runtime.effective_room("lounge")
        sig = eff.presence.zones["desk_kwn"].signals[-1]
        assert sig.source == "opnsense" and sig.host == "kwn-laptop.lan" and sig.name == "kwn_laptop"
        # the YAML-defined config itself is untouched
        assert lux_main.runtime.rooms["lounge"].presence.zones["desk_kwn"].signals == []
        pres = client.get("/api/presence/lounge").json()
        assert any(s["name"] == "kwn_laptop" for s in pres["snapshot"]["signals"])
        assert "signal_health" in pres
        assert client.get("/api/learn/lounge/devices").status_code == 200
