"""The Luxador Standard v1 (docs/STANDARD.md): model validation, the curve, the pipeline, and its invariants."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from luxador.models import RoomInputs
from luxador.parser import load_config_bundle
from luxador.resolver import resolve_room
from luxador.standard import (
    CurveSettings,
    build_house,
    curve_level,
    darkness_from_lux,
    darkness_from_sun,
    locked_hits,
    paths_in,
    suggest_thresholds,
)

CFG = Path(__file__).resolve().parent.parent / "luxador" / "src" / "config"
SETTINGS = {"curve": {}}


@pytest.fixture(scope="module")
def bundle():
    return load_config_bundle(CFG)


def _house(rooms, overrides=None):
    model, errors = build_house(SETTINGS, rooms, overrides)
    assert not errors, errors
    return model


def _eval(bundle, house, now=datetime(2026, 11, 26, 19, 0), **inp):
    schemas, rooms = bundle
    base = dict(occupied=True, ambient_lux=5.0, media_state="off", zones={"desk_kwn": False, "sofa": True}, darkness=1.0)
    base.update(inp)
    return resolve_room(rooms["lounge"], schemas, RoomInputs(**base), now, house=house, overlay_weights={})


# ---------------------------------------------------------------- the model

def test_defaults_match_the_document(bundle):
    _, rooms = bundle
    h = _house(rooms)
    c = h.curve
    assert (c.dark_lux, c.bright_lux, c.min_pct, c.max_pct, c.shape, c.night_max_pct) == (15, 250, 10, 100, 0.8, 35)
    assert h.version == 1
    # ownership from rooms/lounge.yaml
    assert h.owner("lounge", "kwn_desk")[0] == "desk_kwn" and h.owner("lounge", "kwn_desk")[1].mode == "own"
    assert h.owner("lounge", "overhead")[0] == "room"  # unassigned lights belong to the room zone
    assert h.rooms["lounge"].lights["overhead"].soft_start_speed == "slow"


def test_validation_reports_every_error_with_its_path(bundle):
    _, rooms = bundle
    _, errors = build_house(SETTINGS, rooms, {"rooms": {"lounge": {"lights": {"overhead": {"offset_pct": 99, "bogus": 1}}}}})
    assert any("rooms.lounge.lights.overhead.offset_pct" in e for e in errors)
    assert any("bogus" in e for e in errors)
    _, errors = build_house(SETTINGS, rooms, {"rooms": {"lounge": {"zones": {"sofa": {"lights": ["kwn_desk"]}}}}})
    assert any("already owned" in e for e in errors)
    _, errors = build_house(SETTINGS, rooms, {"curve": {"dark_lux": 500, "bright_lux": 100}})
    assert any("bright_lux must be greater" in e for e in errors)
    _, errors = build_house(SETTINGS, rooms, {"rooms": {"lounge": {"zones": {"desk_kwn": {"lights": ["nope"]}}}}})
    assert any("unknown light 'nope'" in e for e in errors)


def test_locks_cover_subtrees():
    patch = {"rooms": {"lounge": {"lights": {"overhead": {"offset_pct": 5, "scale": 1.1}}}}}
    assert paths_in(patch) == ["rooms.lounge.lights.overhead.offset_pct", "rooms.lounge.lights.overhead.scale"]
    assert locked_hits(paths_in(patch), ["rooms.lounge.lights.overhead"]) == paths_in(patch)
    assert locked_hits(paths_in(patch), ["rooms.lounge.lights.shoelight"]) == []


# ---------------------------------------------------------------- the curve

def test_curve_math():
    c = CurveSettings()
    assert darkness_from_lux(250, c) == 0 and darkness_from_lux(15, c) == 1 and darkness_from_lux(1, c) == 1
    mid = darkness_from_lux((15 * 250) ** 0.5, c)  # geometric middle = half dark on the log scale
    assert abs(mid - 0.5) < 0.01
    assert curve_level(0, c) == 0 and curve_level(1, c) == 100
    assert 10 < curve_level(0.5, c) < 100
    assert darkness_from_sun(20) == 0 and darkness_from_sun(-10) == 1 and darkness_from_sun(None) is None


# ---------------------------------------------------------------- the pipeline + invariants

def test_dark_room_lights_up_and_traces_every_stage(bundle):
    _, rooms = bundle
    ev = _eval(bundle, _house(rooms))
    oh = ev.roles["overhead"]
    assert oh.brightness_pct == 100 and oh.zone == "room"
    assert [t["stage"] for t in oh.breakdown.trace] == ["presence", "darkness", "curve", "light", "output"]
    assert ev.roles["shoelight"].brightness_pct == 60  # scale 0.6 from lounge.yaml
    assert ev.roles["kw_desk_led"].brightness_pct == 0  # desk zone owns it, desk empty, idle 0


def test_bright_room_needs_no_light(bundle):
    _, rooms = bundle
    ev = _eval(bundle, _house(rooms), darkness=0.0)
    assert all(r.brightness_pct == 0 for r in ev.roles.values())


def test_own_zone_follows_its_own_presence(bundle):
    _, rooms = bundle
    h = _house(rooms)
    ev = _eval(bundle, h, zones={"desk_kwn": True, "sofa": False})
    assert ev.roles["kw_desk_led"].brightness_pct == 100  # switch mode: on
    h2 = _house(rooms, {"rooms": {"lounge": {"zones": {"desk_kwn": {"idle_pct": 20}}}}})
    ev2 = _eval(bundle, h2, zones={"desk_kwn": False, "sofa": True})
    assert ev2.roles["kwn_desk"].brightness_pct == 20


def test_invariant_nobody_home_means_off(bundle):
    _, rooms = bundle
    ev = _eval(bundle, _house(rooms), occupied=False)
    assert all(r.brightness_pct == 0 for r in ev.roles.values())


def test_invariant_night_cap_and_max(bundle):
    _, rooms = bundle
    h = _house(rooms, {"rooms": {"lounge": {"lights": {"overhead": {"max_pct": 70}}}}})
    assert _eval(bundle, h).roles["overhead"].brightness_pct == 70
    night = _eval(bundle, h, now=datetime(2026, 11, 26, 23, 30))
    assert max(r.brightness_pct for r in night.roles.values()) <= 35


def test_invariant_disabled_and_ignored_are_hands_off(bundle):
    _, rooms = bundle
    schemas, _ = bundle
    h = _house(rooms, {"rooms": {"lounge": {"lights": {"overhead": {"enabled": False}}}}})
    ev = _eval(bundle, h)
    assert ev.roles["overhead"].hands_off
    inp = RoomInputs(occupied=True, ambient_lux=5, darkness=1.0, zones={})
    ev2 = resolve_room(rooms["lounge"], schemas, inp, datetime(2026, 11, 26, 19, 0), house=_house(rooms),
                       overlay_weights={}, pins={"shoelight": {"mode": "ignore"}, "overhead": {"mode": "pinned", "brightness": 33}})
    assert ev2.roles["shoelight"].hands_off and ev2.roles["overhead"].brightness_pct == 33


def test_hands_off_light_is_never_sent_a_command(bundle):
    import asyncio

    from luxador.ha_bridge import HABridge

    _, rooms = bundle
    ev = _eval(bundle, _house(rooms, {"rooms": {"lounge": {"lights": {"overhead": {"enabled": False}}}}}))
    br = HABridge(ha_url="", ha_token="", dry_run=True)
    assert asyncio.run(br.apply_role(ev.roles["overhead"])) == []


def test_legacy_rules_still_work_without_a_house_model(bundle):
    schemas, rooms = bundle
    ev = resolve_room(rooms["lounge"], schemas, RoomInputs(occupied=True, ambient_lux=20, zones={"desk_kwn": True}),
                      datetime(2026, 11, 26, 18, 0))
    assert ev.roles["kwn_desk"].brightness_pct == 80 and ev.roles["kwn_desk"].breakdown.trace == []


# ---------------------------------------------------------------- learning the thresholds

def test_thresholds_learned_from_switch_ons():
    slots = []
    for day in range(12):
        for k, lux in enumerate([400, 300, 200, 120, 60, 30, 20]):
            on = k == 6
            slots.append({"occupied": 1, "lux_in": lux, "light_max": 80 if on else 0})
        slots.append({"occupied": 1, "lux_in": 500, "light_max": 0})
    sug = suggest_thresholds(slots)
    assert sug["enough_data"] and sug["switch_ons"] == 12
    assert sug["dark_lux"] < sug["bright_lux"] and 15 <= sug["dark_lux"] <= 30
    assert suggest_thresholds(slots[:10])["enough_data"] is False


# ---------------------------------------------------------------- API: change, lock, export, import, grid

def test_house_model_api(tmp_path, monkeypatch):
    from luxador import main as lux_main

    cfg = tmp_path / "config"
    (cfg / "rooms").mkdir(parents=True)
    for f in ("settings.yaml", "schemas.yaml", "rooms/lounge.yaml"):
        (cfg / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg)
    monkeypatch.setenv("LUXADOR_DB_PATH", str(tmp_path / "l.db"))
    with TestClient(lux_main.app) as c:
        m = c.get("/api/house/model").json()
        assert m["version"] == 1 and not m["errors"] and len(m["curve_points"]) == 60
        ok = c.post("/api/house/model", json={"patch": {"curve": {"night_max_pct": 25}},
                                              "locks": {"curve.night_max_pct": True}})
        assert ok.status_code == 200 and ok.json()["model"]["curve"]["night_max_pct"] == 25
        assert c.post("/api/house/model", json={"patch": {"curve": {"night_max_pct": 50}}}).status_code == 423
        bad = c.post("/api/house/model", json={"patch": {"rooms": {"lounge": {"lights": {"overhead": {"scale": 9}}}}}})
        assert bad.status_code == 400 and "scale" in str(bad.json())
        exp = c.get("/api/house/model/export").json()
        assert exp["luxador_house_model"] == 1 and exp["locks"] == ["curve.night_max_pct"]
        imp = c.post("/api/house/model/import", json={"overrides": {"curve": {"min_pct": 5}}, "locks": []}).json()
        assert imp["model"]["curve"]["min_pct"] == 5 and imp["model"]["curve"]["night_max_pct"] == 35
        g = c.get("/api/rooms/lounge/grid").json()
        assert g["cells"]["away|dark"]["overhead"] == 0
        assert g["cells"]["room|dark"]["overhead"] > 0 and g["cells"]["room|bright"]["overhead"] == 0
        assert g["cells"]["room|dark"]["kwn_desk"] == 0 and g["cells"]["zone:desk_kwn|dark"]["kwn_desk"] > 0
        st = c.get("/api/rooms/lounge/state").json()
        assert st["evaluation"]["roles"]["overhead"]["breakdown"]["trace"]


def test_thresholds_refuse_degenerate_data():
    slots = []
    for day in range(12):
        slots += [{"occupied": 1, "lux_in": 1.0, "light_max": 0}, {"occupied": 1, "lux_in": 1.0, "light_max": 70}]
    sug = suggest_thresholds(slots)
    assert sug["enough_data"] is False and "near-darkness" in sug["reason"]
