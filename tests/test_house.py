"""Phase 0: Home Assistant areas vs configured rooms (read-only)."""

from pathlib import Path

from luxador.house import classify, house_overview
from luxador.parser import load_config_bundle

CFG = Path(__file__).resolve().parent.parent / "luxador" / "src" / "config"


def test_classify_uses_domain_and_device_class():
    assert classify("light.x", None) == "lights"
    assert classify("binary_sensor.r", {"attributes": {"device_class": "occupancy"}}) == "presence"
    assert classify("binary_sensor.door", {"attributes": {"device_class": "door"}}) is None
    assert classify("sensor.lux", {"attributes": {"device_class": "illuminance"}}) == "lux"
    assert classify("sensor.temp", {"attributes": {"device_class": "temperature"}}) is None


def test_areas_compared_with_rooms():
    _, rooms = load_config_bundle(CFG)
    areas = [{"area_id": "living_room", "name": "Living Room"}, {"area_id": "bedroom", "name": "Bedroom"},
             {"area_id": "garage", "name": "Garage"}]
    devices = [{"id": "dev_radar", "area_id": "living_room"}, {"id": "dev_bed", "area_id": "bedroom"}]
    entities = [
        {"entity_id": "light.living_room_threelight_l", "area_id": "living_room"},
        {"entity_id": "light.shoelight_light", "area_id": "living_room"},
        {"entity_id": "light.new_floor_lamp", "area_id": "living_room"},  # not configured yet
        {"entity_id": "binary_sensor.living_room_ld2450_radar_1_radar_presence", "device_id": "dev_radar"},
        {"entity_id": "light.bedside", "device_id": "dev_bed"},
        {"entity_id": "sensor.bed_lux", "area_id": "bedroom"},
        {"entity_id": "light.hidden", "area_id": "bedroom", "hidden_by": "user"},
        {"entity_id": "switch.garage", "area_id": "garage"},
    ]
    states = {
        "binary_sensor.living_room_ld2450_radar_1_radar_presence": {"attributes": {"device_class": "occupancy"}},
        "sensor.bed_lux": {"attributes": {"device_class": "illuminance"}},
    }
    h = house_overview(areas, devices, entities, states, rooms)
    by = {a["area_id"]: a for a in h["areas"]}
    assert by["living_room"]["room"] == "lounge" and by["living_room"]["status"] == "partly set up"
    assert by["living_room"]["not_in_any_room"]["lights"] == ["light.new_floor_lamp"]
    assert by["bedroom"]["status"] == "not set up" and by["bedroom"]["counts"] == {"lights": 1, "presence": 0, "lux": 1, "media": 0}
    assert "garage" not in by  # nothing lighting-relevant there
    assert "light.kwn_desk" in h["configured_entities_without_area"]


def test_year_endpoint_reachable_and_rooms_list_roles(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from luxador import main as lux_main

    cfg = tmp_path / "config"
    (cfg / "rooms").mkdir(parents=True)
    settings = (CFG / "settings.yaml").read_text(encoding="utf-8").replace('latitude: ""', "latitude: 51.5").replace(
        'longitude: ""', "longitude: -0.12")
    (cfg / "settings.yaml").write_text(settings, encoding="utf-8")
    for f in ("schemas.yaml", "rooms/lounge.yaml"):
        (cfg / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg)
    monkeypatch.setenv("LUXADOR_DB_PATH", str(tmp_path / "l.db"))
    with TestClient(lux_main.app) as client:
        y = client.get("/api/house/year").json()
        assert y["located"] and len(y["days"]) in (365, 366) and y["location_source"] == "settings"
        assert y["now"]["enabled"] is False  # opt-in
        rooms = client.get("/api/rooms").json()["rooms"]
        assert rooms[0]["roles"] and "desk_kwn" in rooms[0]["zones"]
        assert client.get("/api/house/areas").json()["available"] is False  # no HA in tests
        assert client.get("/api/storage").json()["file_mb"] >= 0


def test_seasonal_lift_is_tunable_live(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from luxador import main as lux_main

    cfg = tmp_path / "config"
    (cfg / "rooms").mkdir(parents=True)
    settings = (CFG / "settings.yaml").read_text(encoding="utf-8").replace('latitude: ""', "latitude: 51.5").replace(
        'longitude: ""', "longitude: -0.12")
    (cfg / "settings.yaml").write_text(settings, encoding="utf-8")
    for f in ("schemas.yaml", "rooms/lounge.yaml"):
        (cfg / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg)
    monkeypatch.setenv("LUXADOR_DB_PATH", str(tmp_path / "l.db"))
    with TestClient(lux_main.app) as client:
        y = client.post("/api/house/seasonal", json={"values": {"enabled": True, "winter_boost_max_pct": 25,
                                                                "winter_kelvin_shift": 9999}}).json()
        dec = next(x for x in y["days"] if x["date"].endswith("12-21"))
        assert dec["boost_pct"] == 25.0 and dec["kelvin_shift"] == 1500  # clamped to the safe range
        assert y["now"]["enabled"] is True
        g = client.get("/api/house/seasonal").json()
        assert g["overrides"]["winter_boost_max_pct"] == 25 and g["effective"]["enabled"] is True
        assert client.post("/api/house/seasonal", json={"values": {"bogus": 1}}).status_code == 400
        assert client.post("/api/house/seasonal", json={"values": {"day_start": "21:00", "day_end": "07:00"}}).status_code == 400
        y2 = client.post("/api/house/seasonal", json={"reset": True}).json()
        assert next(x for x in y2["days"] if x["date"].endswith("12-21"))["boost_pct"] == 15.0


def test_seasonal_locks_block_edits_and_survive_reset(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from luxador import main as lux_main

    cfg = tmp_path / "config"
    (cfg / "rooms").mkdir(parents=True)
    settings = (CFG / "settings.yaml").read_text(encoding="utf-8").replace('latitude: ""', "latitude: 51.5").replace(
        'longitude: ""', "longitude: -0.12")
    (cfg / "settings.yaml").write_text(settings, encoding="utf-8")
    for f in ("schemas.yaml", "rooms/lounge.yaml"):
        (cfg / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg)
    monkeypatch.setenv("LUXADOR_DB_PATH", str(tmp_path / "l.db"))
    with TestClient(lux_main.app) as client:
        client.post("/api/house/seasonal", json={"values": {"winter_boost_max_pct": 22}, "locks": {"winter_boost_max_pct": True}})
        assert client.post("/api/house/seasonal", json={"values": {"winter_boost_max_pct": 5}}).status_code == 423
        y = client.post("/api/house/seasonal", json={"reset": True}).json()
        assert next(x for x in y["days"] if x["date"].endswith("12-21"))["boost_pct"] == 22.0  # kept through reset
        assert "math" in y["now"] and y["locks"] == ["winter_boost_max_pct"]
        client.post("/api/house/seasonal", json={"locks": {"winter_boost_max_pct": False}})
        assert client.post("/api/house/seasonal", json={"values": {"winter_boost_max_pct": 5}}).status_code == 200
