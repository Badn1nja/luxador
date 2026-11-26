"""What goes together: co-occurrence, usual times, start clusters, predict-now."""

from datetime import datetime
from pathlib import Path

import pytest

from luxador import datalab as d
from luxador.parser import load_config_bundle
from luxador.together import analyse
from synth import TZ, household

CFG = Path(__file__).resolve().parent.parent / "luxador" / "src" / "config"


@pytest.fixture(scope="module")
def result():
    _, rooms = load_config_bundle(CFG)
    ents = d.room_entities(rooms["lounge"])
    rows = d.normalize_history(household(days=21))
    hist: dict = {}
    for e, ts, st, a in rows:
        hist.setdefault(e, []).append((ts, st, a))
    slots = d.build_slots(ents, hist, d.parse_ts(min(r[1] for r in rows)), d.parse_ts(max(r[1] for r in rows)), TZ)
    return {s["key"]: s for s in analyse(slots, now=datetime(2026, 11, 26, 21, 0))["signals"]}


def test_tv_goes_with_sofa_and_tv_light(result):
    tv = result["tv"]
    goes = {g["key"]: g for g in tv["goes_with"]}
    assert goes["light.loungetv"]["p"] == 1.0 and goes["light.loungetv"]["lift"] > 5
    assert goes["zone.sofa"]["p"] >= 0.9


def test_usual_times_and_prediction(result):
    tv = result["tv"]
    assert tv["usual"]["weekday"][0]["from"] == "20:00" and tv["usual"]["weekday"][0]["to"] == "23:00"
    assert tv["now_p"] >= 0.8  # Thursday 21:00
    assert result["zone.desk_kwn"]["usual"]["weekday"][0]["from"] == "17:00"


def test_multiple_daily_starts_are_not_averaged(result):
    starts = [c["median"] for c in result["light.overhead"]["starts"]["weekday"]]
    assert "07:00" in starts and "17:30" in starts and "12:15" not in starts
