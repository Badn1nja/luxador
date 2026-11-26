"""Human-readable YAML parser for Luxador room maps & compact rules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
import yaml

from .models import (
    BrightnessSpec,
    ConditionSpec,
    OverlayConfig,
    ParsedRule,
    PresenceConfig,
    RoomConfig,
    SignalSpec,
    ZoneConfig,
)
from .schemas import parse_hhmm_minutes


_TARGET_LUX_RE = re.compile(r"^target_lux\((.+)\)$", re.IGNORECASE)
_RAMP_RE = re.compile(r"^ramp\(\s*([0-9.]+)%?\s*->\s*([0-9.]+)%?\s*\)$", re.IGNORECASE)
_DIM_RE = re.compile(r"^(?:dim|offset)\((.+)\)$", re.IGNORECASE)
_ZONE_RE = re.compile(r"^zone\(\s*([a-zA-Z0-9_]+)\s*\)$", re.IGNORECASE)
_STATE_RE = re.compile(r"^state\(\s*([a-zA-Z0-9_ -]+?)\s*\)$", re.IGNORECASE)
_TIME_RE = re.compile(r"^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$")


def _parse_kv_args(arg_str: str) -> tuple[list[float], dict[str, float]]:
    positional: list[float] = []
    kwargs: dict[str, float] = {}
    for raw_part in arg_str.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            kwargs[k.strip().lower()] = float(v.strip().rstrip("%"))
        else:
            positional.append(float(part.rstrip("%")))
    return positional, kwargs


def parse_condition(token: str) -> ConditionSpec:
    """Parse condition field: 'any', 'HH:MM-HH:MM', or 'zone(name)'."""
    clean = token.strip()
    if clean.lower() in ("any", "*"):
        return ConditionSpec(kind="any", raw=clean)
    if m := _ZONE_RE.match(clean):
        return ConditionSpec(kind="zone", zone_name=m.group(1), raw=clean)
    if m := _STATE_RE.match(clean):
        return ConditionSpec(kind="state", state_name=m.group(1).strip().lower(), raw=clean)
    if m := _TIME_RE.match(clean):
        return ConditionSpec(
            kind="time",
            start_minutes=parse_hhmm_minutes(m.group(1)),
            end_minutes=parse_hhmm_minutes(m.group(2)),
            raw=clean,
        )
    raise ValueError(f"Unsupported rule condition syntax: {token!r}")


def parse_brightness(token: str) -> BrightnessSpec:
    """Parse brightness field: '75%', 'off', 'target_lux(...)', 'ramp(a -> b)', 'dim(-60%, floor=0)'."""
    clean = token.strip()
    low = clean.lower()
    if low == "off":
        return BrightnessSpec(kind="off", value=0.0, raw=clean)

    if m := _TARGET_LUX_RE.match(clean):
        pos, kw = _parse_kv_args(m.group(1))
        if not pos:
            raise ValueError(f"target_lux requires target value: {token!r}")
        return BrightnessSpec(
            kind="target_lux",
            value=pos[0],
            min_pct=kw.get("min", 0.0),
            max_pct=kw.get("max", 100.0),
            raw=clean,
        )

    if m := _RAMP_RE.match(clean):
        return BrightnessSpec(
            kind="ramp",
            value=float(m.group(1)),
            end_value=float(m.group(2)),
            raw=clean,
        )

    if m := _DIM_RE.match(clean):
        pos, kw = _parse_kv_args(m.group(1))
        if not pos:
            raise ValueError(f"dim/offset requires percentage delta: {token!r}")
        return BrightnessSpec(
            kind="dim",
            value=pos[0],
            floor_pct=kw.get("floor", 0.0),
            raw=clean,
        )

    # Fixed percentage e.g. '80%' or '80'
    num = float(clean.rstrip("%"))
    if num <= 0:
        return BrightnessSpec(kind="off", value=0.0, raw=clean)
    return BrightnessSpec(kind="fixed", value=max(0.0, min(100.0, num)), raw=clean)


def parse_compact_rule(
    line: str,
    priority: int = 0,
    source_layer: str = "base",
) -> ParsedRule:
    """Parse 'ROLE :: CONDITION :: BRIGHTNESS :: SCHEMA' into a ParsedRule."""
    parts = [p.strip() for p in line.split("::")]
    if len(parts) != 4:
        raise ValueError(
            f"Rule must have 4 '::' sections (ROLE :: CONDITION :: BRIGHTNESS :: SCHEMA): {line!r}"
        )
    role, cond_tok, bri_tok, schema_tok = parts
    if not role or not schema_tok:
        raise ValueError(f"Empty role or schema in rule: {line!r}")

    return ParsedRule(
        role=role,
        condition=parse_condition(cond_tok),
        brightness=parse_brightness(bri_tok),
        schema_name=schema_tok,
        priority=priority,
        source_layer=source_layer,
        raw=line,
    )


def _parse_signals(raw: Any, where: str) -> list[SignalSpec]:
    """Signals are a mapping `name: {source, entity|host, ...}` or `name: entity_id` shorthand."""
    if not raw:
        return []
    if not isinstance(raw, dict):
        raise ValueError(f"'signals' in {where} must be a mapping of name -> spec")
    out: list[SignalSpec] = []
    for name, spec in raw.items():
        if isinstance(spec, str):
            out.append(SignalSpec(name=str(name), source="ha", entity=spec))
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"Signal {name!r} in {where} must be an entity id or a mapping")
        source = str(spec.get("source", "opnsense" if "host" in spec else "ha"))
        sig = SignalSpec(name=str(name), source=source, **{k: v for k, v in spec.items() if k != "source"})
        if sig.source == "ha" and not sig.entity:
            raise ValueError(f"HA signal {name!r} in {where} needs 'entity'")
        if sig.source == "opnsense" and not sig.host:
            raise ValueError(f"OPNsense signal {name!r} in {where} needs 'host' (MAC, IP or hostname)")
        out.append(sig)
    return out


def _parse_zone(name: str, raw: Any) -> ZoneConfig:
    """Zone = shorthand entity string, or `{presence: [...], signals: {...}}`."""
    if isinstance(raw, str):
        return ZoneConfig(name=name, presence=[raw])
    if not isinstance(raw, dict):
        raise ValueError(f"Zone {name!r} must be an entity id or a mapping")
    pres = raw.get("presence", [])
    if isinstance(pres, str):
        pres = [pres]
    return ZoneConfig(
        name=name,
        presence=[str(p) for p in pres],
        signals=_parse_signals(raw.get("signals"), f"zone {name}"),
    )


def load_schemas_yaml(yaml_text: str) -> dict[str, dict[str, Any]]:
    """Parse schemas.yaml text into the schemas mapping."""
    data = yaml.safe_load(yaml_text) or {}
    schemas = data.get("schemas", {})
    if not isinstance(schemas, dict):
        raise ValueError("'schemas' must be a YAML mapping")
    return schemas


def load_room_yaml(yaml_text: str) -> RoomConfig:
    """Parse a room YAML file containing bulbs, compact rules, and overlays."""
    data = yaml.safe_load(yaml_text) or {}
    if not isinstance(data, dict) or "room" not in data:
        raise ValueError("Room YAML must define top-level 'room' identifier")

    raw_bulbs = data.get("bulbs", {})
    bulbs: dict[str, list[str]] = {}
    for role, target in raw_bulbs.items():
        if isinstance(target, str):
            bulbs[role] = [target]
        elif isinstance(target, list):
            bulbs[role] = [str(x) for x in target]
        else:
            raise ValueError(f"Invalid bulb target for role {role!r}")

    base_rules = [
        parse_compact_rule(line, priority=0, source_layer="base")
        for line in data.get("rules", [])
    ]

    overlays: list[OverlayConfig] = []
    for ov in data.get("overlays", []):
        ov_name = ov["name"]
        ov_prio = int(ov.get("priority", 20))
        ov_rules = [
            parse_compact_rule(line, priority=ov_prio, source_layer=ov_name)
            for line in ov.get("apply", [])
        ]
        overlays.append(
            OverlayConfig(
                name=ov_name,
                when=ov["when"],
                priority=ov_prio,
                time_warp_minutes=int(ov.get("time_warp_minutes", 0)),
                blend_minutes=float(ov.get("blend_minutes", 4.0)),
                rules=ov_rules,
            )
        )

    pres_raw = data.get("presence", {}) or {}
    zones: dict[str, ZoneConfig] = {}
    for zname, zraw in (pres_raw.get("zones") or {}).items():
        zones[str(zname)] = _parse_zone(str(zname), zraw)
    presence = PresenceConfig(
        sensors=[str(s) for s in pres_raw.get("sensors", [])],
        clear_delay_seconds=int(pres_raw.get("clear_delay_seconds", 180)),
        zones=zones,
        signals=_parse_signals(pres_raw.get("signals"), "room"),
    )

    return RoomConfig(
        room=str(data["room"]),
        friendly_name=str(data.get("friendly_name", data["room"])),
        presence=presence,
        lux_sensor=data.get("lux_sensor"),
        media_sensor=data.get("media_sensor"),
        bedtime_entity=data.get("bedtime_entity", "input_boolean.bedtime_mode"),
        bulb_lux_gain={str(k): float(v) for k, v in data.get("bulb_lux_gain", {}).items()},
        punch_half_life_minutes=float(data.get("punch_half_life_minutes", 45.0)),
        bulbs=bulbs,
        traits={str(k): [str(t) for t in v] for k, v in (data.get("traits") or {}).items()},
        tweaks=dict(data.get("tweaks") or {}),
        rules=base_rules,
        overlays=overlays,
    )


def load_config_bundle(config_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, RoomConfig]]:
    """Load schemas.yaml and all rooms/*.yaml from a config directory."""
    schemas_path = config_dir / "schemas.yaml"
    schemas = load_schemas_yaml(schemas_path.read_text(encoding="utf-8")) if schemas_path.exists() else {}

    rooms: dict[str, RoomConfig] = {}
    rooms_dir = config_dir / "rooms"
    if rooms_dir.exists():
        for path in sorted(rooms_dir.glob("*.yaml")):
            room = load_room_yaml(path.read_text(encoding="utf-8"))
            rooms[room.room] = room
    return schemas, rooms
