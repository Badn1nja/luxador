"""Tests for Target-Lux solver, punching-bag decay, TV time-warp, and room resolver."""

from datetime import datetime, timedelta
from pathlib import Path

from luxador.elastic import compute_punch_residual, solve_target_lux_brightness
from luxador.models import PunchState, RoomInputs
from luxador.parser import load_config_bundle
from luxador.resolver import resolve_room, simulate_day


def _bundle():
    return load_config_bundle(Path(__file__).resolve().parent.parent / "config")


def test_target_lux_dims_when_daylight_high():
    bright_day = solve_target_lux_brightness(target_lux=160, ambient_lux=400, min_pct=0, max_pct=85, bulb_lux_gain=85)
    dark_room = solve_target_lux_brightness(target_lux=160, ambient_lux=10, min_pct=0, max_pct=85, bulb_lux_gain=85)
    assert bright_day == 0
    assert dark_room == 85


def test_punching_bag_decays_with_half_life():
    t0 = datetime(2026, 11, 26, 20, 0)
    punch = PunchState(role="shoelight", delta_pct=40.0, pushed_at=t0, half_life_minutes=45.0)
    assert compute_punch_residual(punch, t0) == 40.0
    assert round(compute_punch_residual(punch, t0 + timedelta(minutes=45)), 1) == 20.0
    assert compute_punch_residual(punch, t0 + timedelta(hours=5)) == 0.0


def test_tv_warps_time_and_isolates_desk_lights():
    schemas, rooms = _bundle()
    lounge = rooms["lounge"]
    now = datetime(2026, 11, 26, 18, 0)

    inputs = RoomInputs(
        occupied=True,
        ambient_lux=20.0,
        media_state="playing",
        zones={"desk_kwn": True, "sofa": True},
        entity_states={"light.kwn_desk": {"state": "unavailable", "attributes": {}}},
    )
    ev = resolve_room(lounge, schemas, inputs, now)
    assert "tv_watching" in ev.active_overlays
    assert ev.effective_time == datetime(2026, 11, 26, 18, 20)
    # Desks stay on functional 'sun'
    assert ev.roles["kwn_desk"].color.schema_name == "sun"
    assert ev.roles["kwn_desk"].brightness_pct == 80
    assert "light.kwn_desk" in ev.roles["kwn_desk"].offline_entities
    # Soft TV overlay: no colour scene change, accents stay on the room curve
    assert ev.roles["shoelight"].color.schema_name == "sun"
    assert ev.roles["loungetv"].color.schema_name == "sun"
    assert ev.roles["loungetv"].brightness_pct == 60


def test_overlay_conditions_days_times_entities():
    from luxador.resolver import evaluate_overlay_condition as ev

    fri_1730 = datetime(2026, 11, 27, 17, 30)  # Friday
    thu_1730 = datetime(2026, 11, 26, 17, 30)
    party = "switch.luxie_party_lounge == on or day == fri and time >= 17:00"
    off = RoomInputs(entity_states={"switch.luxie_party_lounge": {"state": "off"}})
    on = RoomInputs(entity_states={"switch.luxie_party_lounge": {"state": "on"}})
    assert ev(party, off, fri_1730)
    assert not ev(party, off, thu_1730)
    assert ev(party, on, thu_1730)
    assert not ev(party, off, datetime(2026, 11, 27, 16, 59))
    assert ev("day == weekend", off, datetime(2026, 11, 29, 9, 0))
    assert ev("time in 22:00-02:00", off, datetime(2026, 11, 26, 1, 0))
    assert not ev("time in 22:00-02:00", off, datetime(2026, 11, 26, 3, 0))
    assert ev("zone.sofa == on and not zone.desk == on", RoomInputs(zones={"sofa": True}), thu_1730)
    assert ev("media == playing", RoomInputs(media_state="playing"))


def test_overlay_blends_in_instead_of_snapping():
    schemas, rooms = _bundle()
    lounge = rooms["lounge"]
    now = datetime(2026, 11, 26, 19, 0)
    inputs = RoomInputs(occupied=True, ambient_lux=5.0, media_state="playing")
    off = resolve_room(lounge, schemas, inputs, now, overlay_weights={"tv_watching": 0.0})
    half = resolve_room(lounge, schemas, inputs, now, overlay_weights={"tv_watching": 0.5})
    full = resolve_room(lounge, schemas, inputs, now, overlay_weights={"tv_watching": 1.0})
    a, b, c = (e.roles["overhead"].brightness_pct for e in (off, half, full))
    assert a > b > c  # overhead eases down
    assert abs(b - (a + c) / 2) <= 2
    assert half.effective_time == datetime(2026, 11, 26, 19, 10)  # half the 20m warp
    assert "easing" in " ".join(half.roles["overhead"].breakdown.notes)


def test_simulate_day_returns_96_steps():
    schemas, rooms = _bundle()
    pts = simulate_day(rooms["lounge"], schemas, datetime(2026, 11, 26))
    assert len(pts) == 96
