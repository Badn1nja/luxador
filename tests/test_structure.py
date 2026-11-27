"""The house structure lives in the database: one-time migration, rooms, zones, light groups, validation."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lantern.echo import EchoStore
from lantern.house import area_entities
from lantern.structure import Structure, StructureError, propose_room

CFG = Path(__file__).resolve().parent.parent / "lantern" / "src" / "config"


@pytest.fixture()
def st(tmp_path):
    s = Structure(EchoStore(tmp_path / "e.db"))
    s.migrate(CFG)
    return s


def test_migration_imports_yaml_once(tmp_path):
    s = Structure(EchoStore(tmp_path / "e.db"))
    done = s.migrate(CFG)
    assert any("room lounge" in d for d in done) and any("schemas" in d for d in done)
    assert s.migrated() and s.migrate(CFG) == []  # never twice
    schemas, rooms, errors = s.bundle()
    assert not errors and "lounge" in rooms and "sun" in schemas
    assert "traits" not in s.room_raw("lounge") and "tweaks" not in s.room_raw("lounge")


def test_rooms_add_remove_and_lights_belong_to_one_room(st):
    cfg = st.add_room("bedroom", "Bedroom", {"bedside": ["light.bedside"]}, ["binary_sensor.bed_motion"], "sensor.bed_lux")
    assert cfg.lux_sensor == "sensor.bed_lux" and cfg.bulbs == {"bedside": ["light.bedside"]}
    with pytest.raises(StructureError, match="already belong to room"):
        st.add_room("hall", "Hall", {"x": ["light.shoelight_light"]}, [])
    with pytest.raises(StructureError, match="already exists"):
        st.add_room("bedroom", "Again", {}, [])
    with pytest.raises(StructureError, match="room id"):
        st.add_room("Bad Name!", "", {}, [])
    st.remove_room("bedroom")
    assert "bedroom" not in st.rooms_raw()


def test_zones_add_move_remove(st):
    cfg = st.add_zone("lounge", "tv_corner", lights=["shoelight"])  # no sensor: a named group that joins the room
    assert cfg.presence.zones["tv_corner"].mode == "join" and cfg.presence.zones["tv_corner"].lights == ["shoelight"]
    cfg = st.move_light("lounge", "loungetv", "tv_corner")
    assert "loungetv" in cfg.presence.zones["tv_corner"].lights and "loungetv" not in cfg.presence.zones["sofa"].lights
    cfg = st.update_zone("lounge", "tv_corner", presence=["binary_sensor.tv_radar"], mode="own", idle_pct=15)
    z = cfg.presence.zones["tv_corner"]
    assert z.mode == "own" and z.idle_pct == 15 and z.presence == ["binary_sensor.tv_radar"]
    cfg = st.remove_zone("lounge", "tv_corner")
    assert "tv_corner" not in cfg.presence.zones  # its lights now follow the room
    with pytest.raises(StructureError):
        st.add_zone("lounge", "room")


def test_light_groups_merge_and_split(st):
    cfg = st.merge_lights("lounge", ["loungetv", "mb_desk"], "tv_and_strip")
    assert cfg.bulbs["tv_and_strip"] == ["light.loungetv", "light.wled_strip"] and "loungetv" not in cfg.bulbs
    assert "tv_and_strip" in cfg.presence.zones["sofa"].lights  # keeps the zone of the group it came from
    cfg = st.split_lights("lounge", "tv_and_strip")
    assert "loungetv" in cfg.bulbs and "wled_strip" in cfg.bulbs and "tv_and_strip" not in cfg.bulbs
    with pytest.raises(StructureError):
        st.split_lights("lounge", "shoelight")  # a single light can't be split


def test_proposal_from_an_ha_area():
    devices = [{"id": "d1", "area_id": "bed"}]
    entities = [{"entity_id": "light.bedside", "device_id": "d1"}, {"entity_id": "light.ceiling", "area_id": "bed"},
                {"entity_id": "binary_sensor.bed_radar_presence", "area_id": "bed"},
                {"entity_id": "binary_sensor.bed_radar_zone_1", "area_id": "bed"},
                {"entity_id": "sensor.bed_lux", "area_id": "bed"}, {"entity_id": "light.elsewhere", "area_id": "hall"}]
    states = {"binary_sensor.bed_radar_presence": {"attributes": {"device_class": "occupancy"}},
              "binary_sensor.bed_radar_zone_1": {"attributes": {"device_class": "occupancy"}},
              "sensor.bed_lux": {"attributes": {"device_class": "illuminance"}}}
    ents = area_entities("bed", devices, entities, states)
    assert ents["lights"] == ["light.bedside", "light.ceiling"]
    prop = propose_room("Bedroom", ents, states, {"lounge"})
    assert prop["room"] == "bedroom" and prop["lights"] == {"bedside": ["light.bedside"], "ceiling": ["light.ceiling"]}
    assert prop["presence"] == ["binary_sensor.bed_radar_presence"] and prop["lux_sensor"] == "sensor.bed_lux"
    assert prop["zones"]["zone_1"]["presence"] == ["binary_sensor.bed_radar_zone_1"]


def test_structure_api_and_settings_resume_after_readd(tmp_path, monkeypatch):
    from lantern import main as lux_main

    cfg = tmp_path / "config"
    (cfg / "rooms").mkdir(parents=True)
    for f in ("settings.yaml", "schemas.yaml", "rooms/lounge.yaml"):
        (cfg / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg)
    monkeypatch.setenv("LANTERN_DB_PATH", str(tmp_path / "l.db"))
    with TestClient(lux_main.app) as c:
        assert "lounge" in c.get("/api/house/structure").json()["rooms"]
        (cfg / "rooms" / "lounge.yaml").write_text("room: lounge\nfriendly_name: Edited file\n", encoding="utf-8")
        r = c.post("/api/house/rooms", json={"room": "bedroom", "friendly_name": "Bedroom", "lights": {"bedside": ["light.bedside"]}})
        assert r.status_code == 200
        assert {x["room"] for x in c.get("/api/rooms").json()["rooms"]} == {"lounge", "bedroom"}
        c.post("/api/house/model", json={"patch": {"rooms": {"bedroom": {"lights": {"bedside": {"scale": 0.5}}}}}})
        assert c.delete("/api/house/rooms/bedroom").status_code == 200
        assert {x["room"] for x in c.get("/api/rooms").json()["rooms"]} == {"lounge"}
        assert c.get("/api/house/model").json()["errors"] == []  # its stored settings don't break the model
        c.post("/api/house/rooms", json={"room": "bedroom", "friendly_name": "Bedroom", "lights": {"bedside": ["light.bedside"]}})
        assert c.get("/api/house/model").json()["model"]["rooms"]["bedroom"]["lights"]["bedside"]["scale"] == 0.5  # resumed
        assert c.post("/api/house/rooms/lounge/zones", json={"name": "Bad Zone"}).status_code == 400
        assert c.post("/api/house/rooms/lounge/lights/move", json={"light": "shoelight", "zone": "sofa"}).status_code == 200
        logs = c.get("/api/luxie/lounge/logs").json()["causal_logs"]
        assert any("shoelight now belongs to sofa" in entry["reason"] for entry in logs)
    with TestClient(lux_main.app) as c:  # restart: the edited YAML file is ignored, the database wins
        assert c.get("/api/house/structure").json()["rooms"]["lounge"]["friendly_name"] != "Edited file"


def test_day_chart_endpoint(tmp_path, monkeypatch):
    from lantern import main as lux_main

    monkeypatch.setenv("LANTERN_DB_PATH", str(tmp_path / "l.db"))
    cfg = tmp_path / "config"
    (cfg / "rooms").mkdir(parents=True)
    for f in ("settings.yaml", "schemas.yaml", "rooms/lounge.yaml"):
        (cfg / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg)
    with TestClient(lux_main.app) as c:
        d = c.get("/api/rooms/lounge/day").json()
        assert len(d["points"]) == 96 and {"sun", "gate", "light", "room_lux_pct"} <= set(d["points"][0])
        assert max(p["gate"] for p in d["points"]) > 50 and min(p["gate"] for p in d["points"]) == 0  # night closes, day opens
