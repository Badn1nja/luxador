"""Sensor profiles: validation, cleaning maths, and where each entity is attached."""

import pytest

from lantern import sensors as sm


def test_profile_validation():
    p = sm.make_profile("outdoor", floor_lux=2)
    assert p["preset"] == "outdoor" and p["floor_lux"] == 2.0 and p["bright_lux"] == 30000.0
    with pytest.raises(sm.ProfileError, match="unknown preset"):
        sm.make_profile("nope")
    with pytest.raises(sm.ProfileError, match="unknown setting"):
        sm.make_profile("raw", gain=3)
    with pytest.raises(sm.ProfileError, match="bright_lux must be greater"):
        sm.make_profile("raw", dark_lux=500, bright_lux=100)
    with pytest.raises(sm.ProfileError, match="between"):
        sm.make_profile("raw", scale=0)


def test_relative_brightness_is_log_scaled_and_clamped():
    p = sm.make_profile("indoor_room")  # 15 lx dark, 250 lx bright
    assert sm.relative_brightness(5, p) == 0.0 and sm.relative_brightness(5000, p) == 100.0
    mid = sm.relative_brightness((15 * 250) ** 0.5, p)  # geometric middle of a log scale
    assert 49.5 <= mid <= 50.5


def test_clean_rejects_faults_and_keeps_last_good_value():
    p = sm.make_profile("indoor_room")
    assert not sm.clean("unavailable", p, prev=40.0)["valid"]
    bad = sm.clean(99999, p, prev=40.0)  # outside the valid range
    assert not bad["valid"] and bad["lux"] == 40.0 and bad["relative"] is not None
    ok = sm.clean(100, p)
    assert ok["valid"] and ok["lux"] == 100.0


def test_clean_floor_scale_and_smoothing():
    p = sm.make_profile("raw", floor_lux=10, scale=2)
    assert sm.clean(60, p)["lux"] == 100.0  # (60-10)*2
    s = sm.make_profile("raw", smooth_seconds=100)
    r = sm.clean(200, s, prev=0.0, dt_seconds=100)  # one time constant: ~63% of the way
    assert 120 < r["lux"] < 130


def test_attachments_say_where_everything_is():
    rooms = {"lounge": {"friendly_name": "Lounge", "bulbs": {"lamp": ["light.lamp"], "strip": ["light.strip"]},
                        "lux_sensor": "sensor.lux", "media_sensor": None,
                        "presence": {"sensors": ["binary_sensor.radar"],
                                     "zones": {"desk": {"presence": ["binary_sensor.desk"], "lights": ["strip"], "lux_sensor": "sensor.desk_lux"}}}}}
    where = sm.attachments(rooms, ["sensor.outdoor_lux"])
    assert where["sensor.outdoor_lux"] == ["home"]
    assert where["light.lamp"] == ["Lounge"] and where["light.strip"] == ["Lounge › desk"]
    assert where["binary_sensor.radar"] == ["Lounge"] and where["sensor.desk_lux"] == ["Lounge › desk"]
