"""Core domain models for El Luxador & Luxie."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


class ColorOutput(BaseModel):
    """Resolved color target for a light or role."""

    mode: Literal["kelvin", "rgb", "keep"] = "kelvin"
    kelvin: int | None = None
    rgb: tuple[int, int, int] | None = None
    stage: str = "steady"
    schema_name: str = "sun"


class BrightnessSpec(BaseModel):
    """Parsed brightness expression from a compact rule."""

    kind: Literal["fixed", "target_lux", "ramp", "dim", "off"]
    value: float = 0.0
    min_pct: float = 0.0
    max_pct: float = 100.0
    end_value: float | None = None
    floor_pct: float = 0.0
    raw: str = ""


class ConditionSpec(BaseModel):
    """Parsed condition / time-window expression from a compact rule."""

    kind: Literal["any", "time", "zone", "state"] = "any"
    start_minutes: int | None = None
    end_minutes: int | None = None
    zone_name: str | None = None
    state_name: str | None = None
    raw: str = "any"


class ParsedRule(BaseModel):
    """Single compiled rule from `ROLE :: CONDITION :: BRIGHTNESS :: SCHEMA`."""

    role: str
    condition: ConditionSpec
    brightness: BrightnessSpec
    schema_name: str
    priority: int = 0
    source_layer: str = "base"
    raw: str = ""


class OverlayConfig(BaseModel):
    """Conditional higher-priority rule group (e.g. TV watching, bedtime)."""

    name: str
    when: str
    priority: int = 20
    time_warp_minutes: int = 0
    blend_minutes: float = 4.0  # ease in/out over this long instead of snapping
    rules: list[ParsedRule] = Field(default_factory=list)


class SignalSpec(BaseModel):
    """One piece of evidence attached to a room or zone (HA entity or OPNsense network host).

    Active rules (HA):  `states: [playing, paused]`, or numeric `above:` / `below:`,
    otherwise on/home/playing/open/true count as active.
    Active rules (OPNsense): host seen on the LAN, and if `active_kbps` is set, traffic >= that rate.
    """

    name: str
    source: Literal["ha", "opnsense"] = "ha"
    entity: str | None = None
    host: str | None = None  # MAC, IP or hostname as OPNsense reports it
    states: list[str] = Field(default_factory=list)
    above: float | None = None
    below: float | None = None
    active_kbps: float | None = None


class ZoneConfig(BaseModel):
    """A sub-area of a room: presence sensors that say someone is there + signals that say what they're doing."""

    name: str
    presence: list[str] = Field(default_factory=list)
    signals: list[SignalSpec] = Field(default_factory=list)
    lights: list[str] = Field(default_factory=list)  # light groups this zone owns (Standard §1)
    mode: str | None = None  # join | own (None = standard default)
    idle_pct: float | None = None


class PresenceConfig(BaseModel):
    """Room > Zone presence and signal mappings."""

    sensors: list[str] = Field(default_factory=list)
    clear_delay_seconds: int = 180
    zones: dict[str, ZoneConfig] = Field(default_factory=dict)
    signals: list[SignalSpec] = Field(default_factory=list)  # room-level signals (not tied to a zone)


class RoomConfig(BaseModel):
    """Complete parsed room specification."""

    room: str
    friendly_name: str = ""
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    lux_sensor: str | None = None
    media_sensor: str | None = None
    bedtime_entity: str | None = "input_boolean.bedtime_mode"
    standard: dict[str, Any] = Field(default_factory=dict)  # `standard:` block: engine + per-light settings
    bulb_lux_gain: dict[str, float] = Field(default_factory=dict)
    punch_half_life_minutes: float = 45.0
    bulbs: dict[str, list[str]] = Field(default_factory=dict)
    traits: dict[str, list[str]] = Field(default_factory=dict)
    tweaks: dict[str, Any] = Field(default_factory=dict)
    rules: list[ParsedRule] = Field(default_factory=list)
    overlays: list[OverlayConfig] = Field(default_factory=list)


class PunchState(BaseModel):
    """Active user override ('punching-bag' decaying delta) on a role."""

    role: str
    delta_pct: float
    pushed_at: datetime
    half_life_minutes: float = 45.0
    actor: str = "user"


class BrightnessBreakdown(BaseModel):
    """Transparent causal breakdown answering 'Why am I at X%?'."""

    base_pct: float = 0.0
    time_warp_minutes: int = 0
    overlay_delta_pct: float = 0.0
    learned_baseline_pct: float = 0.0
    active_punch_pct: float = 0.0
    final_pct: int = 0
    rule_matched: str = "unoccupied/off"
    bucket_key: str = "default"
    notes: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)  # Standard §4: one entry per pipeline stage


class RoleEvaluation(BaseModel):
    """Evaluated target for one room role."""

    role: str
    entities: list[str]
    on: bool
    brightness_pct: int
    color: ColorOutput
    breakdown: BrightnessBreakdown
    offline_entities: list[str] = Field(default_factory=list)
    hands_off: bool = False  # disabled or 'ignore' pinned: never send a command
    zone: str | None = None
    transition_s: int | None = None


class RoomInputs(BaseModel):
    """Live or simulated environment inputs for evaluating a room."""

    occupied: bool = True
    ambient_lux: float = 120.0
    media_state: str = "off"
    bedtime: bool = False
    zones: dict[str, bool] = Field(default_factory=dict)
    entity_states: dict[str, dict[str, Any]] = Field(default_factory=dict)
    idle_minutes: float = 0.0
    signature: str = ""
    learned_state: str | None = None
    learned_match: str | None = None  # "exact" | "approx" | None
    lux_source: str = "sensor"
    warnings: list[str] = Field(default_factory=list)
    sun_elev: float | None = None
    darkness: float | None = None  # smoothed darkness from the runtime (Standard §4 stage 2)


class StateConfidence(BaseModel):
    """Luxie's confidence estimate for inferred room state."""

    inferred_state: str
    state_source: str = "heuristic"  # "learned" when it came from a pattern you named
    sleep_confidence: float
    working_confidence: float
    tv_confidence: float
    needs_confirmation: bool = False
    prompt_text: str | None = None


class RoomEvaluation(BaseModel):
    """Complete evaluation snapshot for a room."""

    room: str
    timestamp: datetime
    effective_time: datetime
    active_overlays: list[str] = Field(default_factory=list)
    roles: dict[str, RoleEvaluation] = Field(default_factory=dict)
    confidence: StateConfidence
