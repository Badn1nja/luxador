"""Color schema evaluation: Kelvin/RGB curves, -blue protection, and animated sequences."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .models import ColorOutput


def parse_hhmm_minutes(hhmm: str) -> int:
    """Convert 'HH:MM' into minutes since midnight [0, 1439]."""
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM timestamp: {hhmm!r}")
    hours, mins = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23 and 0 <= mins <= 59):
        raise ValueError(f"Out of range HH:MM timestamp: {hhmm!r}")
    return hours * 60 + mins


def kelvin_to_rgb(kelvin: int) -> tuple[int, int, int]:
    """Approximate RGB tuple for a correlated color temperature in Kelvin."""
    temp = max(1000, min(12000, kelvin)) / 100.0
    if temp <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(temp) - 161.1195681661
        blue = 0.0 if temp <= 19 else 138.5177312231 * math.log(temp - 10) - 305.0447927307
    else:
        red = 329.698727446 * ((temp - 60) ** -0.1332047592)
        green = 288.1221695283 * ((temp - 60) ** -0.0755148492)
        blue = 255.0
    return (
        int(max(0, min(255, round(red)))),
        int(max(0, min(255, round(green)))),
        int(max(0, min(255, round(blue)))),
    )


def lerp_rgb(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Linearly interpolate between two RGB colors for t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


def _point_to_rgb(point: dict[str, Any]) -> tuple[int, int, int]:
    if "rgb" in point:
        r, g, b = point["rgb"]
        return (int(r), int(g), int(b))
    return kelvin_to_rgb(int(point.get("kelvin", 3000)))


def evaluate_curve(
    schema_name: str,
    points: dict[str, dict[str, Any]],
    minute_of_day: float,
) -> ColorOutput:
    """Smoothly interpolate a 24h cyclic curve (handles midnight wrap cleanly)."""
    if not points:
        return ColorOutput(mode="kelvin", kelvin=3000, rgb=kelvin_to_rgb(3000), schema_name=schema_name)

    ordered = sorted((parse_hhmm_minutes(k), v) for k, v in points.items())
    if len(ordered) == 1:
        _, pt = ordered[0]
        if "rgb" in pt and "kelvin" not in pt:
            rgb = _point_to_rgb(pt)
            return ColorOutput(mode="rgb", rgb=rgb, schema_name=schema_name)
        k = int(pt.get("kelvin", 3000))
        return ColorOutput(mode="kelvin", kelvin=k, rgb=kelvin_to_rgb(k), schema_name=schema_name)

    # Locate surrounding keypoints around the 1440-minute cycle
    m = minute_of_day % 1440.0
    prev_m, prev_pt = ordered[-1]
    next_m, next_pt = ordered[0]
    span = (next_m + 1440.0) - prev_m
    pos = (m - prev_m) if m >= prev_m else (m + 1440.0 - prev_m)

    for idx in range(len(ordered) - 1):
        m1, pt1 = ordered[idx]
        m2, pt2 = ordered[idx + 1]
        if m1 <= m <= m2:
            prev_m, prev_pt = m1, pt1
            next_m, next_pt = m2, pt2
            span = max(1.0, float(m2 - m1))
            pos = m - m1
            break

    t = max(0.0, min(1.0, pos / span))

    # If both endpoints are Kelvin, interpolate in Kelvin so color_temp bulbs stay native
    if "kelvin" in prev_pt and "kelvin" in next_pt and "rgb" not in prev_pt and "rgb" not in next_pt:
        kelvin = int(round(prev_pt["kelvin"] + (next_pt["kelvin"] - prev_pt["kelvin"]) * t))
        return ColorOutput(mode="kelvin", kelvin=kelvin, rgb=kelvin_to_rgb(kelvin), schema_name=schema_name)

    rgb = lerp_rgb(_point_to_rgb(prev_pt), _point_to_rgb(next_pt), t)
    return ColorOutput(mode="rgb", rgb=rgb, schema_name=schema_name)


def evaluate_palette(
    schema_name: str,
    colors: list[list[int] | tuple[int, int, int]],
    interval_seconds: float,
    elapsed_seconds: float,
    stage: str = "animated",
) -> ColorOutput:
    """Evaluate a smooth cyclic animated color palette."""
    if not colors:
        return ColorOutput(mode="rgb", rgb=(255, 160, 80), stage=stage, schema_name=schema_name)
    interval = max(1.0, float(interval_seconds))
    total = len(colors)
    phase = (elapsed_seconds / interval) % total
    idx = int(phase)
    nxt = (idx + 1) % total
    frac = phase - idx
    c1 = (int(colors[idx][0]), int(colors[idx][1]), int(colors[idx][2]))
    c2 = (int(colors[nxt][0]), int(colors[nxt][1]), int(colors[nxt][2]))
    return ColorOutput(mode="rgb", rgb=lerp_rgb(c1, c2, frac), stage=stage, schema_name=schema_name)


def evaluate_schema(
    schema_name: str,
    schemas: dict[str, dict[str, Any]],
    effective_time: datetime,
    elapsed_in_mode_seconds: float = 0.0,
    prior_color: ColorOutput | None = None,
) -> ColorOutput:
    """Resolve a color schema name, inline hex/kelvin literal, or 'keep'."""
    token = schema_name.strip()
    if token.lower() == "keep":
        if prior_color is not None:
            return prior_color.model_copy(update={"stage": "kept"})
        return ColorOutput(mode="keep", stage="kept", schema_name="keep")

    # Inline Kelvin like '2700K'
    if token.upper().endswith("K") and token[:-1].isdigit():
        k = int(token[:-1])
        return ColorOutput(mode="kelvin", kelvin=k, rgb=kelvin_to_rgb(k), schema_name=token)

    # Inline Hex like '#ff6600'
    if token.startswith("#") and len(token) == 7:
        rgb = (int(token[1:3], 16), int(token[3:5], 16), int(token[5:7], 16))
        return ColorOutput(mode="rgb", rgb=rgb, schema_name=token)

    spec = schemas.get(token)
    if not spec:
        return ColorOutput(mode="kelvin", kelvin=3000, rgb=kelvin_to_rgb(3000), schema_name=token)

    kind = spec.get("type", "curve")
    minute_of_day = effective_time.hour * 60 + effective_time.minute + effective_time.second / 60.0

    if kind == "curve":
        return evaluate_curve(token, spec.get("points", {}), minute_of_day)

    if kind == "animated_palette":
        return evaluate_palette(
            token,
            spec.get("colors", []),
            spec.get("interval_seconds", 12),
            elapsed_in_mode_seconds,
            stage="animated",
        )

    if kind == "sequence":
        trans_in = spec.get("transition_in", {})
        dur = max(0.0, float(trans_in.get("duration_seconds", 8)))
        target_in = _point_to_rgb(trans_in)
        if elapsed_in_mode_seconds < dur and dur > 0:
            start_rgb = (
                prior_color.rgb
                if prior_color and prior_color.rgb is not None
                else kelvin_to_rgb(prior_color.kelvin or 3000)
                if prior_color
                else target_in
            )
            frac = elapsed_in_mode_seconds / dur
            return ColorOutput(
                mode="rgb",
                rgb=lerp_rgb(start_rgb, target_in, frac),
                stage=f"transition_in ({int(frac * 100)}%)",
                schema_name=token,
            )
        then_spec = spec.get("then", {})
        if then_spec.get("type") == "animated_palette":
            return evaluate_palette(
                token,
                then_spec.get("colors", []),
                then_spec.get("interval_seconds", 12),
                max(0.0, elapsed_in_mode_seconds - dur),
                stage="animated",
            )
        return ColorOutput(mode="rgb", rgb=target_in, stage="steady", schema_name=token)

    return ColorOutput(mode="kelvin", kelvin=3000, rgb=kelvin_to_rgb(3000), schema_name=token)
