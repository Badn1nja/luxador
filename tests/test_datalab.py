"""Phase 1 · Data: traceback import, 5-minute grid, intent labels, model, arbitration, predictions."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from luxador import datalab as d
from luxador.ha_bridge import HABridge
from luxador.luxie import LuxieStore
from luxador.parser import load_config_bundle
from synth import ENT, TZ, household

CFG = Path(__file__).resolve().parent.parent / "luxador" / "src" / "config"


@pytest.fixture(scope="module")
def slots():
    _, rooms = load_config_bundle(CFG)
    ents = d.room_entities(rooms["lounge"])
    rows = d.normalize_history(household())
    hist: dict = {}
    for e, ts, st, a in rows:
        hist.setdefault(e, []).append((ts, st, a))
    start = d.parse_ts(min(r[1] for r in rows))
    end = d.parse_ts(max(r[1] for r in rows))
    out = d.build_slots(ents, hist, start, end, TZ)
    d.label_slots(out)
    d.add_since_active(out)
    return out


def test_normalize_history_keeps_useful_attrs_and_minimal_responses():
    raw = [
        {"entity_id": "light.a", "state": "on", "last_changed": "2026-11-26T07:00:00+00:00",
         "attributes": {"brightness": 128, "friendly_name": "A", "color_temp_kelvin": 2700}},
        {"state": "off", "last_changed": "2026-11-26T08:00:00+00:00"},  # minimal response: entity omitted
    ]
    rows = d.normalize_history(raw)
    assert rows[0][0] == rows[1][0] == "light.a"
    assert "friendly_name" not in rows[0][3] and "brightness" in rows[0][3]


def test_statistics_accept_epoch_ms_and_iso():
    rows = d.normalize_statistics({"sensor.lux": [
        {"start": 1_764_000_000_000, "mean": 10.0, "min": 1.0, "max": 20.0},
        {"start": "2026-11-26T07:00:00+00:00", "mean": 5.0},
    ]})
    assert len(rows) == 2 and rows[0][1].startswith("2025-11-24")


def test_grid_reads_lights_presence_and_tv(slots):
    s = next(x for x in slots if x["slot"] == "2026-11-24T20:30")  # Tuesday, TV on
    assert s["media"] == 1 and s["occupied"] == 1
    assert s["lights"]["loungetv"] == 60.0 and s["lights"]["overhead"] == 29.8  # 30% -> 77/255
    assert "sofa" in s["zones"] and s["sun_elev"] is not None


def test_rule_labels_find_sleep_nightlight_wake(slots):
    by = {s["slot"]: s for s in slots}
    assert by["2026-11-24T03:00"]["intent"] in ("sleep", "nightlight")
    trip_nights = [s for s in slots if s["intent"] == "nightlight"]
    assert trip_nights and all(s["minute_of_day"] // 60 == 3 for s in trip_nights)
    assert by["2026-11-24T07:00"]["intent"] == "wake"
    assert by["2026-11-24T12:00"]["intent"] == "away"
    assert by["2026-11-24T21:00"]["intent"] == "evening"
    assert by["2026-11-24T07:45"]["intent"] in ("wake", "day")  # dark morning is not evening


def test_sleep_wake_predictions(slots):
    sw = d.sleep_wake_times(slots)
    assert sw["wake"]["weekday"]["median"] == "07:00"
    assert sw["wake"]["weekend"]["median"] == "09:30"
    assert sw["sleep"]["weekday"]["median"] == "23:15"


def test_model_beats_time_only_baseline_and_reads_night(slots):
    ev = d.evaluate(slots)
    assert ev["enough_data"] and ev["accuracy"] >= ev["baseline_time_only"]
    m = d.IntentModel().fit(slots)
    night = m.predict(3, "weekday", {"sun": "night", "occupied": "0", "media": "0", "since_active": "<6h", "lux_out": "na"})
    assert next(iter(night)) == "sleep"
    rt = d.IntentModel.from_json(m.to_json())
    assert rt.predict(3, "weekday", {"sun": "night"}) == m.predict(3, "weekday", {"sun": "night"})


def test_arbitration_layers_intents():
    pol = d.DEFAULT_INTENT_POLICY
    assert d.arbitrate({"evening": 0.54, "wake": 0.46}, pol)["intent"] == "wake"  # the day starting wins
    assert d.arbitrate({"sleep": 0.62, "nightlight": 0.38}, pol)["intent"] == "sleep"
    assert d.arbitrate({"nightlight": 0.5, "sleep": 0.5}, pol)["intent"] == "nightlight"  # sleep below its 60 %
    assert d.arbitrate({"day": 0.2, "away": 0.2, "evening": 0.25}, pol)["intent"] == "evening"  # fallback: most probable


def test_user_labels_override_rules(slots):
    import copy

    s2 = copy.deepcopy(slots)
    d.label_slots(s2, [{"start": "2026-11-24T21:00", "end": "2026-11-24T22:00", "intent": "sleep"}])
    by = {s["slot"]: s for s in s2}
    assert by["2026-11-24T21:30"]["intent"] == "sleep" and by["2026-11-24T21:30"]["intent_source"] == "user"


def test_traceback_and_series_cover_the_drop(slots):
    tb = d.traceback_profile(slots)
    tue = tb["heatmap_weekday_hour"][1]
    assert tue[21] is not None and tue[3] is not None and tue[21] > tue[3]
    assert tb["roles"]["overhead"]["weekday"][18] > tb["roles"]["overhead"]["weekday"][21]  # evening dim
    ser = d.series(slots, "2026-11-24T00:00", "2026-11-25T00:00", bucket_minutes=5)
    assert len(ser["t"]) == 288 and ser["intent"][36] in ("sleep", "nightlight", "wake")
    month = d.series(slots, "2026-11-10T00:00", "2026-11-27T00:00", bucket_minutes=30)
    assert len(month["t"]) < 900 and "overhead" in month["lights"]


def test_bridge_recognises_its_own_changes():
    br = HABridge(ha_url="http://ha", ha_token="t", dry_run=False)
    br.own_user_id = "luxador-user"
    assert br.is_own_change("light.a", {"user_id": "luxador-user"}, 40)
    assert not br.is_own_change("light.a", {"user_id": "someone"}, 40)
    import time
    br._recent_cmd["light.a"] = (time.monotonic(), 55)
    assert br.is_own_change("light.a", {}, 54)
    assert not br.is_own_change("light.a", {}, 80)


def test_datalab_end_to_end_through_api(tmp_path, monkeypatch):
    from luxador import main as lux_main

    cfg_dst = tmp_path / "config"
    (cfg_dst / "rooms").mkdir(parents=True)
    for f in ("settings.yaml", "schemas.yaml", "intents.yaml"):
        (cfg_dst / f).write_text((CFG / f).read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dst / "rooms" / "lounge.yaml").write_text((CFG / "rooms" / "lounge.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg_dst)
    monkeypatch.setenv("LUXADOR_DB_PATH", str(tmp_path / "luxie.db"))

    with TestClient(lux_main.app) as client:
        rt = lux_main.runtime
        rt.settings["time_zone"] = "Europe/London"
        rt.luxie.store_history(d.normalize_history(household()))
        cov = client.post("/api/datalab/lounge/rebuild").json()
        assert cov["slots"] > 3000
        summ = client.get("/api/datalab/lounge").json()
        assert summ["sleep_wake"]["wake"]["weekday"]["median"] == "07:00"
        assert summ["evaluation"]["enough_data"]
        assert "live_intent" in summ
        ser = client.get("/api/datalab/lounge/series", params={"range": "week", "end": "2026-11-26T12:00"}).json()
        assert ser["bucket_minutes"] == 15 and ser["t"]
        bad = client.post("/api/datalab/lounge/labels", json={"start": "2026-11-24T21:00", "end": "2026-11-24T22:00", "intent": "party"})
        assert bad.status_code == 400
        ok = client.post("/api/datalab/lounge/labels", json={"start": "2026-11-24T21:00", "end": "2026-11-24T22:00", "intent": "sleep"})
        assert ok.status_code == 200
        assert any(l["source"] == "user" for l in ok.json()["labels"])
        # live capture is thinned for chatty numeric sensors
        now = datetime.now(timezone.utc)
        for k in range(5):
            rt.datalab.capture(ENT["lux"], {"state": "100", "last_changed": (now + timedelta(seconds=k)).isoformat()})
        assert rt.luxie.history_span()["by_source"].get("live") == 1


def test_states_are_discovered_not_defined(slots):
    import copy

    from luxador.states import assign_state, discover_states

    s2 = copy.deepcopy(slots)
    m = discover_states(s2, max_states=28)
    assert 4 <= m["k"] <= 28
    sts = {st["id"]: st for st in m["states"]}
    # the TV evening shows up on its own, with each light's real behaviour attached
    tv = [st for st in m["states"] if "TV on" in st["description"]]
    assert tv and all(st["intent"] == "evening" for st in tv)
    assert any(st["lights"].get("loungetv", {}).get("median_when_on") == 60.0 for st in tv)
    # light levels are not part of the context, so they cannot define a state
    assert all(len(st["centroid"]) == len(m["states"][0]["centroid"]) for st in m["states"])
    # every usable slot got a state, and a live context maps back onto one
    assert all(s.get("state") is not None for s in s2 if s.get("light_max") is not None)
    sid = assign_state(m, {"minute_of_day": 21 * 60, "daytype": "weekday", "sun_elev": -30, "occupied": 1,
                           "zones": ["sofa"], "media": 1, "_since_active": 0})
    assert "TV on" in sts[sid]["description"]
    # capped by max_states
    assert discover_states(copy.deepcopy(slots), max_states=5)["k"] <= 5


def test_state_names_survive_rebuild(tmp_path):
    from luxador.datalab import DataLab

    _, rooms = load_config_bundle(CFG)
    store = LuxieStore(tmp_path / "t.db")
    store.store_history(d.normalize_history(household()))

    class NoHA:
        configured = False
        time_zone = "Europe/London"
        states: dict = {}

    lab = DataLab(store, rooms, {"time_zone": "Europe/London"}, NoHA())
    lab.rebuild("lounge")
    tv = next(st for st in lab.states("lounge")["states"] if "TV on" in st["description"])
    lab.name_state("lounge", tv["id"], "Telly time")
    lab.rebuild("lounge")
    assert any(st["name"] == "Telly time" for st in lab.states("lounge")["states"])
