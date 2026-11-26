"""Elastic brightness math: Target-Lux compensation, TV time-warp, and punching-bag decay."""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import BrightnessSpec, ConditionSpec, PunchState


def time_in_window(minute_of_day: float, start_m: int, end_m: int) -> tuple[bool, float]:
    """Check if minute_of_day falls in [start_m, end_m] (supporting midnight wrap) and return progress [0, 1]."""
    m = minute_of_day % 1440.0
    if start_m <= end_m:
        if start_m <= m <= end_m:
            span = max(1.0, float(end_m - start_m))
            return True, (m - start_m) / span
        return False, 0.0

    # Midnight wrap (e.g. 21:00 -> 02:00)
    span = max(1.0, float((1440 - start_m) + end_m))
    if m >= start_m:
        return True, (m - start_m) / span
    if m <= end_m:
        return True, ((1440.0 - start_m) + m) / span
    return False, 0.0


def apply_time_warp(now: datetime, warp_minutes: int) -> datetime:
    """Shift effective clock forward/backward for elastic event timing (e.g. TV on early)."""
    if warp_minutes == 0:
        return now
    return now + timedelta(minutes=warp_minutes)


def compute_punch_residual(punch: PunchState | None, now: datetime) -> float:
    """Compute remaining decaying user override offset ('punching bag')."""
    if punch is None:
        return 0.0
    elapsed_mins = max(0.0, (now - punch.pushed_at).total_seconds() / 60.0)
    half_life = max(1.0, punch.half_life_minutes)
    residual = punch.delta_pct * (0.5 ** (elapsed_mins / half_life))
    return 0.0 if abs(residual) < 1.5 else residual


def solve_target_lux_brightness(
    target_lux: float,
    ambient_lux: float,
    min_pct: float,
    max_pct: float,
    bulb_lux_gain: float,
    current_bulb_pct: float = 0.0,
    deadband_lux: float = 15.0,
) -> float:
    """Estimate brightness % needed to reach target_lux without feedback oscillation.

    Subtracts estimated current artificial contribution of this role from the measured
    room lux to recover true ambient daylight, then maps deficit against lux_gain.
    """
    gain = max(5.0, float(bulb_lux_gain))
    estimated_self_lux = gain * (max(0.0, min(100.0, current_bulb_pct)) / 100.0)
    estimated_ambient = max(0.0, ambient_lux - estimated_self_lux)
    deficit = target_lux - estimated_ambient

    if deficit <= -deadband_lux:
        return min_pct
    if deficit <= 0:
        # Inside deadband: gently hold near min_pct
        return min_pct

    needed_pct = (deficit / gain) * 100.0
    return max(min_pct, min(max_pct, needed_pct))


def evaluate_brightness_spec(
    spec: BrightnessSpec,
    condition: ConditionSpec,
    effective_time: datetime,
    ambient_lux: float,
    bulb_lux_gain: float,
    current_bulb_pct: float = 0.0,
    base_pct_for_dim: float = 0.0,
) -> float:
    """Evaluate raw rule brightness before learned offsets and user punches."""
    if spec.kind == "off":
        return 0.0
    if spec.kind == "fixed":
        return spec.value
    if spec.kind == "ramp":
        start_val = spec.value
        end_val = spec.end_value if spec.end_value is not None else spec.value
        progress = 0.0
        if condition.kind == "time" and condition.start_minutes is not None and condition.end_minutes is not None:
            m = effective_time.hour * 60 + effective_time.minute + effective_time.second / 60.0
            _, progress = time_in_window(m, condition.start_minutes, condition.end_minutes)
        return start_val + (end_val - start_val) * progress
    if spec.kind == "target_lux":
        return solve_target_lux_brightness(
            target_lux=spec.value,
            ambient_lux=ambient_lux,
            min_pct=spec.min_pct,
            max_pct=spec.max_pct,
            bulb_lux_gain=bulb_lux_gain,
            current_bulb_pct=current_bulb_pct,
        )
    if spec.kind == "dim":
        # Negative value reduces brightness by %, positive raises it
        factor = 1.0 + (spec.value / 100.0)
        adjusted = base_pct_for_dim * factor
        if adjusted < 3.0 and spec.floor_pct == 0.0:
            return 0.0
        return max(spec.floor_pct, min(100.0, adjusted))
    return 0.0
