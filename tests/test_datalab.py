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
    for f in ("settings.yaml", "schemas.yaml"):
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
        assert "evaluation" not in summ and "live_state" in summ
        ser = client.get("/api/datalab/lounge/series", params={"range": "week", "end": "2026-11-26T12:00"}).json()
        assert ser["bucket_minutes"] == 15 and ser["t"]
        assert "predicted" not in ser and ser["state"]
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


def test_retention_prunes_and_keeps_last_state(tmp_path):
    store = LuxieStore(tmp_path / "t.db")
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(days=210)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    store.store_history([("light.a", older, "on", "{}"), ("light.a", old, "off", "{}"), ("light.a", new, "on", "{}")])
    store.log_causal_event("lounge", "overhead", 0, 50, "x", "y", ts=datetime.now(timezone.utc) - timedelta(days=40))
    out = store.apply_retention()
    assert out["ha_history"] == 1 and out["causal_log"] == 1
    kept = store.load_history(["light.a"])["light.a"]
    assert [r[1] for r in kept] == ["off", "on"]  # last pre-cutoff state survives so the grid starts right
    rep = store.storage_report()
    assert rep["rows"]["ha_history"] == 2 and "file_mb" in rep


def test_aged_slots_fold_into_seasonal_once(slots, tmp_path):
    from luxador.datalab import DataLab

    store = LuxieStore(tmp_path / "t.db")
    store.replace_slots("lounge", slots)
    lab = DataLab(store, {}, {"time_zone": "Europe/London"}, None)
    cutoff = "2026-11-20T00:00"
    n = lab.fold_aged_slots("lounge", cutoff)
    assert n == sum(1 for s in slots if s["slot"] < cutoff)
    assert lab.fold_aged_slots("lounge", cutoff) == 0  # never double counted
    summ = d.seasonal_summary(store.load_seasonal("lounge"))
    assert summ["weeks_covered"] >= 1 and summ["years"] == [2026]
    wk = summ["weeks"][-1]
    assert 0 < wk["home_share"] < 1 and wk["bri_when_on"] > 0


def test_yearly_sun_model_and_compensation():
    from zoneinfo import ZoneInfo

    from luxador.seasonal import SeasonalConfig, SeasonalModel, sun_elevation

    ldn = ZoneInfo("Europe/London")
    assert 61 < sun_elevation(51.5, -0.12, datetime(2026, 6, 21, 13, 0, tzinfo=ldn)) < 63
    m = SeasonalModel(51.5, -0.12, "Europe/London", SeasonalConfig(enabled=True))
    v = m.year_view(2026)
    assert len(v["days"]) == 365 and len(v["hourly_elev"][0]) == 24
    dec = next(x for x in v["days"] if x["date"] == "2026-12-21")
    jun = next(x for x in v["days"] if x["date"] == "2026-06-21")
    assert dec["sunset"].startswith("15:5") and dec["boost_pct"] == 15.0 and dec["evening_shift_min"] == 0
    assert jun["boost_pct"] == 0 and jun["evening_shift_min"] == 45
    assert m.now(datetime(2026, 12, 21, 22, 0, tzinfo=ldn))["boost_now_pct"] == 0  # outside the day window
    syd = SeasonalModel(-33.87, 151.2, "Australia/Sydney", SeasonalConfig()).year_view(2026)
    assert next(x for x in syd["days"] if x["date"] == "2026-06-21")["boost_pct"] > 10  # southern winter


def test_resolver_applies_seasonal_only_when_enabled():
    from luxador.resolver import resolve_room

    schemas, rooms = load_config_bundle(CFG)
    now = datetime(2026, 12, 21, 19, 0)
    inp = d_inputs = None
    from luxador.models import RoomInputs

    inp = RoomInputs(occupied=True, ambient_lux=5.0, media_state="off")
    base = resolve_room(rooms["lounge"], schemas, inp, now)
    season = {"applies": True, "boost_now_pct": 15.0, "kelvin_now": 300, "warp_minutes": 0,
              "day": {"day_hours": 7.8}}
    lifted = resolve_room(rooms["lounge"], schemas, inp, now, seasonal=season)
    a, b = base.roles["overhead"].brightness_pct, lifted.roles["overhead"].brightness_pct
    assert a > 0 and b == min(100, round(a * 1.15))
    assert any("Winter lift" in n for n in lifted.roles["overhead"].breakdown.notes)
    off = resolve_room(rooms["lounge"], schemas, inp, now, seasonal={**season, "applies": False})
    assert off.roles["overhead"].brightness_pct == a


def _lab(tmp_path):
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
    return lab


def test_switchboard_merge_unmerge_and_links_survive_rebuild(tmp_path):
    lab = _lab(tmp_path)
    before = lab.states("lounge")
    sleepers = [s["id"] for s in before["states"] if s["intent"] == "sleep"][:3]
    merged = lab.merge_states("lounge", sleepers)
    assert len(merged["states"]) == len(before["states"]) - (len(sleepers) - 1)
    big = max(merged["states"], key=lambda s: s["merged_from"])
    assert big["merged_from"] == len(sleepers)

    lab.link_state("lounge", big["id"], "shoelight", "pinned", brightness=5, kelvin=2200)
    lab.name_state("lounge", big["id"], "Deep night")
    lab.rebuild("lounge")  # numbers may change; links, names and merges must not
    again = next(s for s in lab.states("lounge")["states"] if s["merged_from"] == len(sleepers))
    assert again["links"]["shoelight"] == {"mode": "pinned", "brightness": 5.0, "kelvin": 2200, "locked": False}
    assert again["name"] == "Deep night"

    ds = lab.state_dataset("lounge", again["id"])
    assert sum(ds["hours"]) == ds["slots"] > 0 and ds["sun"].get("night", 0) > 0.5

    lab.link_state("lounge", again["id"], "shoelight", "learned")
    assert "shoelight" not in lab._state("lounge", again["id"])["links"]
    after = lab.unmerge_state("lounge", again["id"])
    assert len(after["states"]) == len(before["states"])

    with pytest.raises(ValueError):
        lab.link_state("lounge", 0, "nope", "off")
    with pytest.raises(ValueError):
        lab.merge_states("lounge", [0])


def test_locked_links_resist_changes(tmp_path):
    lab = _lab(tmp_path)
    sid = lab.states("lounge")["states"][0]["id"]
    lab.link_state("lounge", sid, "overhead", "pinned", brightness=40, locked=True)
    with pytest.raises(PermissionError):
        lab.link_state("lounge", sid, "overhead", "off")
    lab.link_state("lounge", sid, "overhead", "pinned", brightness=40, locked=False)  # unlock
    lab.link_state("lounge", sid, "overhead", "off")
    assert lab._state("lounge", sid)["links"]["overhead"]["mode"] == "off"
