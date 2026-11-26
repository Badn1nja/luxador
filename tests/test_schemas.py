"""Tests for color schema evaluation (Kelvin curves, -blue RGB, TV sequence)."""

from datetime import datetime
from pathlib import Path

from luxador.parser import load_schemas_yaml
from luxador.schemas import evaluate_schema


def _load_default_schemas():
    p = Path(__file__).resolve().parent.parent / "luxador" / "src" / "config" / "schemas.yaml"
    return load_schemas_yaml(p.read_text(encoding="utf-8"))


def test_sun_curve_day_vs_evening():
    schemas = _load_default_schemas()
    noon = evaluate_schema("sun", schemas, datetime(2026, 11, 26, 12, 0))
    late = evaluate_schema("sun", schemas, datetime(2026, 11, 26, 23, 0))
    assert noon.mode == "kelvin" and noon.kelvin == 4800
    assert late.mode == "kelvin" and late.kelvin == 2400


def test_no_blue_cuts_blue_channel_at_night():
    schemas = _load_default_schemas()
    night = evaluate_schema("no_blue", schemas, datetime(2026, 11, 26, 23, 0))
    assert night.mode == "rgb"
    assert night.rgb is not None
    r, _g, b = night.rgb
    assert r > 200 and b <= 20


def test_tv_vivid_transitions_then_animates():
    schemas = _load_default_schemas()
    t0 = evaluate_schema("tv_vivid", schemas, datetime(2026, 11, 26, 20, 0), elapsed_in_mode_seconds=2.0)
    assert "transition_in" in t0.stage

    t_anim = evaluate_schema("tv_vivid", schemas, datetime(2026, 11, 26, 20, 0), elapsed_in_mode_seconds=20.0)
    assert t_anim.stage == "animated"
    assert t_anim.rgb is not None
