"""Flint's lux model: lamp gains, daylight vs sun, the sensor-unavailable fallback, Apply, and computed sun."""

import math
import random
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from lantern import datalab as d
from lantern.lux import fit_daylight, learn, predict_daylight
from lantern.models import RoomInputs
from lantern.parser import load_config_bundle
from lantern.resolver import resolve_room, standard_darkness
from lantern.standard import build_house
from synth import TZ, household

CFG = Path(__file__).resolve().parent.parent / "lantern" / "src" / "config"


def _synthetic(n=3000, seed=1):
    random.seed(seed)
    roles = ["overhead", "shoelight", "desk"]
    truth = {"overhead": 60.0, "shoelight": 9.0, "desk": 22.0}
    slots = []
    for i in range(n):
        sun = -40 + 85 * (0.5 + 0.5 * math.sin(i / 50))
        lv = {r: random.choice([0, 0, 30, 60, 100]) for r in roles}
        day = 0 if sun <= 0 else 400 * math.sin(math.radians(sun)) ** 1.3
        lux = 1.5 + sum(truth[r] * lv[r] / 100 for r in roles) + day + random.gauss(0, 1)
        slots.append({"sun_elev": sun, "lux_in": max(0, lux), "lights": lv})
    return slots, roles, truth


def test_lamp_gains_and_sensor_floor_are_recovered():
    slots, roles, truth = _synthetic()
    m = learn(slots, roles)
    assert m["lamps"]["ok"] and m["lamps"]["r2"] > 0.99
    for r in roles:
        assert abs(m["lamps"]["gains"][r] - truth[r]) < 1.5
    assert abs(m["lamps"]["base_lux"] - 1.5) < 0.5


def test_daylight_curve_is_recovered():
    slots, roles, _ = _synthetic()
    m = learn(slots, roles)
    day = m["daylight"]
    assert day["ok"] and abs(day["B"] - 1.3) < 0.1 and abs(day["A"] - 400) < 40
    assert predict_daylight(day, -5) == 0 and predict_daylight(day, 30) > 100
    assert m["curve"] and len(m["scatter"]) <= 401


def test_not_enough_data_is_said_not_guessed():
    assert learn([], ["a"])["lamps"]["ok"] is False
    assert fit_daylight([(10.0, 50.0)])["ok"] is False


def test_lamps_that_always_switch_together_are_reported():
    slots = [{"sun_elev": -20, "lux_in": 5 + 30 * (k % 2), "lights": {"a": 100 * (k % 2), "b": 100 * (k % 2)}} for k in range(100)]
    m = learn(slots, ["a", "b"])["lamps"]
    assert m["ok"] and m["gains"] == {"a": None, "b": None}
    assert m["together"] == [{"lights": ["a", "b"], "combined_gain": 30.0}]


def test_darkness_uses_learned_gain_floor_and_daylight_fallback():
    schemas, rooms = load_config_bundle(CFG)
    cfg = rooms["lounge"]
    house, errs = build_house({}, rooms, {"rooms": {"lounge": {
        "lux_base": 2.0, "daylight": {"A": 400, "B": 1.3}, "lights": {"overhead": {"lux_gain": 100}}}}})
    assert not errs
    states = {e: {"state": "on", "attributes": {"brightness": 255}} for e in cfg.bulbs["overhead"]}
    inp = RoomInputs(occupied=True, ambient_lux=110.0, lux_source="sensor", entity_states=states, sun_elev=-20)
    d1, note = standard_darkness(house, cfg, inp)
    assert "own lights 100" in note and "sensor floor 2.0" in note and d1 == 1.0  # 110 - 2 - 100 = 8 lx -> dark
    off = RoomInputs(occupied=True, ambient_lux=0, lux_source="unavailable", sun_elev=40)
    d2, note2 = standard_darkness(house, cfg, off)
    assert "predicted daylight" in note2 and d2 < 0.1  # ~225 lx of predicted daylight: next to no light needed
    night = RoomInputs(occupied=True, ambient_lux=0, lux_source="unavailable", sun_elev=-30)
    assert standard_darkness(house, cfg, night)[0] == 1.0


def test_on_off_plug_is_not_clamped_to_a_brightness():
    schemas, rooms = load_config_bundle(CFG)
    house, _ = build_house({}, rooms, None)
    ev = resolve_room(rooms["lounge"], schemas, RoomInputs(occupied=True, ambient_lux=1, darkness=1.0, zones={"desk_kwn": True}),
                      datetime(2026, 11, 26, 23, 30), house=house, overlay_weights={})
    led = ev.roles["kw_desk_led"]
    assert led.brightness_pct == 100 and not any(t["stage"] == "clamp" for t in led.breakdown.trace)
    assert ev.roles["overhead"].brightness_pct <= 35  # dimmable lights still respect the night cap


def test_echo_computes_the_sun_when_location_is_known():
    _, rooms = load_config_bundle(CFG)
    ents = d.room_entities(rooms["lounge"])
    rows = [r for r in d.normalize_history(household(days=2)) if r[0] != "sun.sun"]  # HA doesn't record elevation
    hist: dict = {}
    for e, ts, st, a in rows:
        hist.setdefault(e, []).append((ts, st, a))
    start, end = d.parse_ts(min(r[1] for r in rows)), d.parse_ts(max(r[1] for r in rows))
    from lantern.seasonal import sun_elevation

    slots = d.build_slots(ents, hist, start, end, TZ, sun_fn=lambda t: sun_elevation(51.5, -0.12, t))
    assert all(s["sun_elev"] is not None for s in slots)
    noon = next(s for s in slots if s["slot"].endswith("T12:00"))
    assert 10 < noon["sun_elev"] < 20  # London, late November


def test_lux_api_and_apply(tmp_path, monkeypatch):
    from lantern import main as lux_main

    cfg = tmp_path / "config"
    (cfg / "rooms").mkdir(parents=True)
    settings = (CFG / "settings.yaml").read_text(encoding="utf-8").replace('latitude: ""', "latitude: 51.5").replace(
        'longitude: ""', "longitude: -0.12").replace('time_zone: ""', 'time_zone: "Europe/London"')
    (cfg / "settings.yaml").write_text(settings, encoding="utf-8")
    for f in ("schemas.yaml", "rooms/lounge.yaml"):
        (cfg / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg)
    monkeypatch.setenv("LANTERN_DB_PATH", str(tmp_path / "l.db"))
    with TestClient(lux_main.app) as c:
        lux_main.runtime.echo.store_history(d.normalize_history(household(days=14)))
        c.post("/api/datalab/lounge/rebuild")
        m = c.get("/api/flint/lounge/lux").json()
        assert m["sensor"]["placement"] == "indoor" and "now" in m
        assert m["lamps"]["ok"] or "reason" in m["lamps"]
        res = c.post("/api/flint/lounge/lux/apply")
        if m["lamps"]["ok"] or m["daylight"]["ok"]:
            assert res.status_code == 200
            applied = c.get("/api/flint/lounge/lux").json()["applied"]
            assert applied["lux_base"] is not None or applied["daylight"] is not None
        else:
            assert res.status_code == 400


def test_a_lamp_with_no_data_does_not_block_the_fit():
    slots, roles, truth = _synthetic()
    for s in slots:
        s["lights"]["dead"] = None  # e.g. a bulb that's been unavailable all along
    m = learn(slots, roles + ["dead"])["lamps"]
    assert m["ok"] and m["no_data"] == ["dead"] and m["gains"]["dead"] is None
    assert abs(m["gains"]["overhead"] - truth["overhead"]) < 1.5
