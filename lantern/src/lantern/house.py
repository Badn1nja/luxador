"""Phase 0 · Structure: HOUSE > ROOM > ZONE.

Reads Home Assistant's own Areas (and the devices/entities assigned to them) and compares them with the rooms
Lantern is configured for. Read-only: it proposes, it never writes config.
"""

from __future__ import annotations

from typing import Any

from .models import RoomConfig

PRESENCE_CLASSES = {"occupancy", "motion", "presence"}


def classify(entity_id: str, state: dict[str, Any] | None) -> str | None:
    """Which kind of lighting-relevant thing an entity is, from its domain and device_class."""
    domain = entity_id.split(".", 1)[0]
    dc = ((state or {}).get("attributes") or {}).get("device_class")
    if domain == "light":
        return "lights"
    if domain == "binary_sensor" and dc in PRESENCE_CLASSES:
        return "presence"
    if domain == "sensor" and dc == "illuminance":
        return "lux"
    if domain == "media_player":
        return "media"
    return None


def room_entities(cfg: RoomConfig) -> set[str]:
    ids: set[str] = set(cfg.presence.sensors)
    for ents in cfg.bulbs.values():
        ids.update(ents)
    for z in cfg.presence.zones.values():
        ids.update(z.presence)
        ids.update(s.entity for s in z.signals if s.entity)
    ids.update(s.entity for s in cfg.presence.signals if s.entity)
    ids.update(e for e in (cfg.lux_sensor, cfg.media_sensor) if e)
    return ids


def house_overview(
    areas: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    states: dict[str, dict[str, Any]],
    rooms: dict[str, RoomConfig],
) -> dict[str, Any]:
    device_area = {d["id"]: d.get("area_id") for d in devices}
    area_names = {a["area_id"]: a.get("name") or a["area_id"] for a in areas}
    by_area: dict[str, dict[str, list[str]]] = {a: {"lights": [], "presence": [], "lux": [], "media": []} for a in area_names}
    for e in entities:
        if e.get("disabled_by") or e.get("hidden_by"):
            continue
        eid = e["entity_id"]
        area = e.get("area_id") or device_area.get(e.get("device_id"))
        kind = classify(eid, states.get(eid))
        if area in by_area and kind:
            by_area[area][kind].append(eid)

    room_sets = {name: room_entities(cfg) for name, cfg in rooms.items()}
    covered: set[str] = set().union(*room_sets.values()) if room_sets else set()
    out = []
    for area_id, kinds in by_area.items():
        all_ids = {e for v in kinds.values() for e in v}
        if not all_ids:
            continue
        overlap = {name: len(all_ids & ents) for name, ents in room_sets.items()}
        room = max(overlap, key=overlap.get) if overlap and max(overlap.values()) > 0 else None
        missing = {k: sorted(e for e in v if e not in covered) for k, v in kinds.items()}
        out.append({
            "area_id": area_id,
            "name": area_names[area_id],
            "room": room,
            "counts": {k: len(v) for k, v in kinds.items()},
            "entities": {k: sorted(v) for k, v in kinds.items()},
            "not_in_any_room": {k: v for k, v in missing.items() if v},
            "status": "set up" if room and not any(missing.values()) else "partly set up" if room else "not set up",
        })
    out.sort(key=lambda a: ({"set up": 0, "partly set up": 1, "not set up": 2}[a["status"]], a["name"]))
    unplaced = sorted(covered - {e for a in out for v in a["entities"].values() for e in v})
    return {
        "areas": out,
        "rooms": sorted(rooms),
        "summary": {
            "areas_with_lights": sum(1 for a in out if a["counts"]["lights"]),
            "set_up": sum(1 for a in out if a["status"] == "set up"),
            "partly": sum(1 for a in out if a["status"] == "partly set up"),
            "not_set_up": sum(1 for a in out if a["status"] == "not set up"),
        },
        "configured_entities_without_area": unplaced,
    }
