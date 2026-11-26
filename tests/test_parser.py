"""Tests for compact YAML rule & room config parser."""

from pathlib import Path
import pytest

from luxador.parser import load_config_bundle, parse_compact_rule


def test_parse_target_lux_and_time_wrap():
    rule = parse_compact_rule("overhead :: 21:00-02:00 :: target_lux(160, min=10, max=85) :: no_blue")
    assert rule.role == "overhead"
    assert rule.condition.kind == "time"
    assert rule.condition.start_minutes == 21 * 60
    assert rule.condition.end_minutes == 2 * 60
    assert rule.brightness.kind == "target_lux"
    assert rule.brightness.value == 160
    assert rule.brightness.min_pct == 10
    assert rule.brightness.max_pct == 85
    assert rule.schema_name == "no_blue"


def test_parse_ramp_dim_and_zone():
    r1 = parse_compact_rule("overhead :: 21:00-02:00 :: ramp(65 -> 12) :: no_blue")
    assert r1.brightness.kind == "ramp"
    assert r1.brightness.value == 65
    assert r1.brightness.end_value == 12

    r2 = parse_compact_rule("overhead :: any :: dim(-65%, floor=0) :: keep")
    assert r2.brightness.kind == "dim"
    assert r2.brightness.value == -65
    assert r2.brightness.floor_pct == 0

    r3 = parse_compact_rule("kwn_desk :: zone(desk_kwn) :: 80% :: sun")
    assert r3.condition.kind == "zone"
    assert r3.condition.zone_name == "desk_kwn"
    assert r3.brightness.value == 80


def test_invalid_rule_raises():
    with pytest.raises(ValueError):
        parse_compact_rule("missing :: sections")


def test_load_lounge_config_bundle():
    cfg_dir = Path(__file__).resolve().parent.parent / "luxador" / "src" / "config"
    schemas, rooms = load_config_bundle(cfg_dir)
    assert "sun" in schemas and "tv_vivid" in schemas
    assert "lounge" in rooms
    lounge = rooms["lounge"]
    assert "overhead" in lounge.bulbs
    assert len(lounge.bulbs["overhead"]) == 3
    assert len(lounge.overlays) >= 2
