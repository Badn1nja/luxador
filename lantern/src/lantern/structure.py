"""The house structure (HOUSE › ROOM › ZONE › LIGHT GROUP, and which sensors belong where), stored in Echo.

The database is the source of truth. Old `rooms/*.yaml` and `schemas.yaml` files are read **once** to migrate an
existing install, then ignored. Every change is validated by parsing the room exactly as the engine will, and
logged. Export/Import of the whole house is the readable, portable form.

A room's stored form is a plain dict (the same shape the old YAML files had), e.g.::

    {"room": "lounge", "friendly_name": "Living Room",
     "presence": {"sensors": [...], "clear_delay_seconds": 180,
                  "zones": {"sofa": {"presence": [...], "lights": ["loungetv"], "mode": "join", "signals": {}}}},
     "lux_sensor": "sensor.x", "media_sensor": "media_player.tv",
     "bulbs": {"overhead": ["light.a", "light.b"], "shoelight": ["light.c"]}, ...}
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from .models import RoomConfig
from .parser import load_schemas_yaml, room_from_dict

ROOM_KIND, SCHEMAS_KIND, META_KIND, HOME_KIND = "room", "schemas", "meta", "home"
ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return (s or "x")[:40]


class StructureError(ValueError):
    pass


def normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Light groups are always lists of entities (old files allowed `group: light.x`)."""
    bulbs = raw.get("bulbs") or {}
    raw["bulbs"] = {g: ([e] if isinstance(e, str) else list(e)) for g, e in bulbs.items()}
    return raw


class Structure:
    def __init__(self, echo: Any, log=None) -> None:
        self.echo = echo
        self._log = log or (lambda room, what: None)

    # ------------------------------------------------------------------ reading
    def rooms_raw(self) -> dict[str, dict[str, Any]]:
        return {k: normalise(v) for k, v in self.echo.get_structure(ROOM_KIND).items()}

    def room_raw(self, room: str) -> dict[str, Any]:
        raw = self.rooms_raw().get(room)
        if raw is None:
            raise KeyError(room)
        return copy.deepcopy(raw)

    def schemas(self) -> dict[str, Any]:
        return self.echo.get_structure(SCHEMAS_KIND).get("all", {})

    def bundle(self) -> tuple[dict[str, Any], dict[str, RoomConfig], list[str]]:
        """(schemas, rooms, errors). A room that fails to parse is skipped and reported, never fatal."""
        rooms, errors = {}, []
        for key, raw in self.rooms_raw().items():
            try:
                rooms[key] = room_from_dict(raw)
            except Exception as exc:
                errors.append(f"room {key}: {exc}")
        return self.schemas(), rooms, errors

    def migrated(self) -> bool:
        return "migrated" in self.echo.get_structure(META_KIND)

    # ------------------------------------------------------------------ one-time migration
    def migrate(self, config_dir: Path, default_schemas_dir: Path | None = None) -> list[str]:
        """Read rooms/*.yaml + schemas.yaml once. Returns what was imported. Never runs twice."""
        if self.migrated():
            return []
        done = []
        rooms_dir = config_dir / "rooms"
        if rooms_dir.exists() and not self.rooms_raw():
            for path in sorted(rooms_dir.glob("*.yaml")):
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                room_from_dict(data)  # must parse before we keep it
                normalise(data)
                data.pop("traits", None)
                data.pop("tweaks", None)
                self.echo.put_structure(ROOM_KIND, str(data["room"]), data)
                done.append(f"room {data['room']} from {path.name}")
        if not self.schemas():
            for d in (config_dir, default_schemas_dir):
                if d and (d / "schemas.yaml").exists():
                    self.echo.put_structure(SCHEMAS_KIND, "all", load_schemas_yaml((d / "schemas.yaml").read_text(encoding="utf-8")))
                    done.append(f"colour schemas from {d / 'schemas.yaml'}")
                    break
        self.echo.put_structure(META_KIND, "migrated", {"items": done})
        return done

    # ------------------------------------------------------------------ writing (every write is validated)
    def _check_lights_unique(self, room: str, raw: dict[str, Any]) -> None:
        mine = {e for ents in (raw.get("bulbs") or {}).values() for e in ents}
        for other, oraw in self.rooms_raw().items():
            if other == room:
                continue
            clash = mine & {e for ents in (oraw.get("bulbs") or {}).values() for e in ents}
            if clash:
                raise StructureError(f"{', '.join(sorted(clash))} already belong to room {other!r}")

    def save_room(self, raw: dict[str, Any], what: str) -> RoomConfig:
        normalise(raw)
        room = str(raw.get("room", ""))
        if not ID_RE.match(room):
            raise StructureError("room id must be 1-40 characters: a-z, 0-9 and _")
        zones = (raw.get("presence") or {}).get("zones") or {}
        owned: dict[str, str] = {}
        for z, zr in zones.items():
            if not ID_RE.match(z) or z == "room":
                raise StructureError(f"zone name {z!r} must be a-z, 0-9 and _ (and not 'room')")
            for light in zr.get("lights") or []:
                if light not in (raw.get("bulbs") or {}):
                    raise StructureError(f"zone {z} owns unknown light group {light!r}")
                if light in owned:
                    raise StructureError(f"light group {light!r} is owned by both {owned[light]} and {z}")
                owned[light] = z
        self._check_lights_unique(room, raw)
        try:
            cfg = room_from_dict(raw)
        except Exception as exc:
            raise StructureError(str(exc)) from exc
        self.echo.put_structure(ROOM_KIND, room, raw)
        self._log(room, what)
        return cfg

    def add_room(self, room: str, friendly_name: str, lights: dict[str, list[str]], presence: list[str],
                 lux_sensor: str | None = None, media_sensor: str | None = None,
                 zones: dict[str, dict[str, Any]] | None = None) -> RoomConfig:
        if room in self.rooms_raw():
            raise StructureError(f"room {room!r} already exists")
        raw = {
            "room": room,
            "friendly_name": friendly_name or room,
            "presence": {"sensors": list(presence), "clear_delay_seconds": 180, "zones": zones or {}},
            "lux_sensor": lux_sensor or None,
            "media_sensor": media_sensor or None,
            "bulbs": {g: list(e) for g, e in lights.items()},
        }
        return self.save_room(raw, f"added room {friendly_name or room}")

    def remove_room(self, room: str) -> None:
        if room not in self.rooms_raw():
            raise KeyError(room)
        self.echo.delete_structure(ROOM_KIND, room)
        self._log(room, "removed room (its lights are hands-off now; history kept)")

    def update_room(self, room: str, **fields: Any) -> RoomConfig:
        raw = self.room_raw(room)
        for k in ("friendly_name", "lux_sensor", "media_sensor"):
            if k in fields and fields[k] is not None:
                raw[k] = fields[k] or None
        if fields.get("presence") is not None:
            raw.setdefault("presence", {})["sensors"] = list(fields["presence"])
        if fields.get("clear_delay_seconds") is not None:
            raw.setdefault("presence", {})["clear_delay_seconds"] = int(fields["clear_delay_seconds"])
        return self.save_room(raw, "changed room settings")

    # ------------------------------------------------------------------ zones
    def add_zone(self, room: str, name: str, presence: list[str] | None = None, lights: list[str] | None = None,
                 mode: str | None = None) -> RoomConfig:
        raw = self.room_raw(room)
        zones = raw.setdefault("presence", {}).setdefault("zones", {})
        if name in zones:
            raise StructureError(f"zone {name!r} already exists")
        for z in zones.values():  # a light moves into the new zone
            z["lights"] = [l for l in (z.get("lights") or []) if l not in (lights or [])]
        zones[name] = {"presence": list(presence or []), "lights": list(lights or []), "signals": {},
                       "mode": mode or ("own" if presence else "join")}
        return self.save_room(raw, f"added zone {name}")

    def update_zone(self, room: str, name: str, **fields: Any) -> RoomConfig:
        raw = self.room_raw(room)
        zones = (raw.get("presence") or {}).get("zones") or {}
        if name not in zones:
            raise KeyError(name)
        z = zones[name]
        if fields.get("presence") is not None:
            z["presence"] = list(fields["presence"])
        if fields.get("mode") is not None:
            if fields["mode"] not in ("join", "own"):
                raise StructureError("mode must be join or own")
            z["mode"] = fields["mode"]
        if fields.get("lux_sensor") is not None:
            z["lux_sensor"] = fields["lux_sensor"] or None
        if fields.get("idle_pct") is not None:
            z["idle_pct"] = max(0.0, min(100.0, float(fields["idle_pct"])))
        if fields.get("lights") is not None:
            for other_name, other in zones.items():
                if other_name != name:
                    other["lights"] = [l for l in (other.get("lights") or []) if l not in fields["lights"]]
            z["lights"] = list(fields["lights"])
        return self.save_room(raw, f"changed zone {name}")

    def move_light(self, room: str, light: str, to_zone: str) -> RoomConfig:
        """Give a light group to a zone ('room' = the room's general zone)."""
        raw = self.room_raw(room)
        if light not in (raw.get("bulbs") or {}):
            raise StructureError(f"unknown light group {light!r}")
        zones = (raw.get("presence") or {}).get("zones") or {}
        if to_zone != "room" and to_zone not in zones:
            raise StructureError(f"unknown zone {to_zone!r}")
        for z in zones.values():
            z["lights"] = [l for l in (z.get("lights") or []) if l != light]
        if to_zone != "room":
            zones[to_zone].setdefault("lights", []).append(light)
        return self.save_room(raw, f"{light} now belongs to {to_zone}")

    def remove_zone(self, room: str, name: str) -> RoomConfig:
        raw = self.room_raw(room)
        zones = (raw.get("presence") or {}).get("zones") or {}
        if name not in zones:
            raise KeyError(name)
        del zones[name]  # its lights fall back to the room zone
        return self.save_room(raw, f"removed zone {name} (its lights now follow the room)")

    # ------------------------------------------------------------------ entities (which things belong where)
    def assign_entity(self, room: str, entity: str, kind: str, zone: str | None = None) -> RoomConfig:
        """Attach an HA entity to a room, or to one of its zones. `kind` is lights/presence/lux/media."""
        raw = self.room_raw(room)
        zones = raw.setdefault("presence", {}).setdefault("zones", {})
        if zone and zone != "room" and zone not in zones:
            raise StructureError(f"unknown zone {zone!r}")
        scoped = bool(zone and zone != "room")
        if self.where_is(raw, entity):
            raise StructureError(f"{entity} is already attached to this room: remove it first to move it")
        if kind == "lights":
            base = slug(entity.split(".", 1)[1]) or "light"
            name, n = base, 2
            while name in (raw.get("bulbs") or {}):
                name, n = f"{base}_{n}", n + 1
            raw.setdefault("bulbs", {})[name] = [entity]
            if scoped:
                zones[zone].setdefault("lights", []).append(name)
            what = f"{entity} added as light {name}" + (f" in zone {zone}" if scoped else "")
        elif kind == "presence":
            if scoped:
                zones[zone].setdefault("presence", []).append(entity)
            else:
                raw["presence"].setdefault("sensors", []).append(entity)
            what = f"presence {entity} attached to " + (f"zone {zone}" if scoped else "the room")
        elif kind in ("lux", "media"):
            if scoped:
                raise StructureError(f"a {kind} sensor belongs to the room, not a zone")
            raw["lux_sensor" if kind == "lux" else "media_sensor"] = entity
            what = f"{kind} sensor {entity} attached to the room"
        else:
            raise StructureError(f"{entity} isn't a light, presence, lux or media entity")
        return self.save_room(raw, what)

    @staticmethod
    def where_is(raw: dict[str, Any], entity: str) -> bool:
        pres = raw.get("presence") or {}
        return (entity in {e for ents in (raw.get("bulbs") or {}).values() for e in ents}
                or entity in (pres.get("sensors") or [])
                or any(entity in (z.get("presence") or []) for z in (pres.get("zones") or {}).values())
                or entity in (raw.get("lux_sensor"), raw.get("media_sensor")))

    def unassign_entity(self, room: str, entity: str) -> RoomConfig:
        raw = self.room_raw(room)
        if not self.where_is(raw, entity):
            raise KeyError(entity)
        pres = raw.setdefault("presence", {})
        zones = pres.setdefault("zones", {})
        pres["sensors"] = [e for e in (pres.get("sensors") or []) if e != entity]
        for z in zones.values():
            z["presence"] = [e for e in (z.get("presence") or []) if e != entity]
        for k in ("lux_sensor", "media_sensor"):
            if raw.get(k) == entity:
                raw[k] = None
        for g in list(raw.get("bulbs") or {}):
            raw["bulbs"][g] = [e for e in raw["bulbs"][g] if e != entity]
            if not raw["bulbs"][g]:
                del raw["bulbs"][g]
                for z in zones.values():
                    z["lights"] = [light for light in (z.get("lights") or []) if light != g]
        return self.save_room(raw, f"{entity} detached from the room")

    # ------------------------------------------------------------------ home (house-wide things)
    def home(self) -> dict[str, Any]:
        h = self.echo.get_structure(HOME_KIND).get("home") or {}
        return {"entities": list(h.get("entities") or []), "activities": list(h.get("activities") or [])}

    def _save_home(self, h: dict[str, Any], what: str) -> dict[str, Any]:
        self.echo.put_structure(HOME_KIND, "home", h)
        self._log("home", what)
        return h

    def home_add_entity(self, entity: str) -> dict[str, Any]:
        h = self.home()
        if entity in h["entities"]:
            raise StructureError(f"{entity} is already attached to the home")
        h["entities"].append(entity)
        return self._save_home(h, f"{entity} attached to the home")

    def home_remove_entity(self, entity: str) -> dict[str, Any]:
        h = self.home()
        if entity not in h["entities"]:
            raise KeyError(entity)
        h["entities"].remove(entity)
        return self._save_home(h, f"{entity} detached from the home")

    def add_activity(self, name: str) -> dict[str, Any]:
        name = slug(name)
        if not ID_RE.match(name):
            raise StructureError("activity name must be 1-40 characters: a-z, 0-9 and _")
        h = self.home()
        if name in h["activities"]:
            raise StructureError(f"activity {name!r} already exists")
        h["activities"].append(name)
        return self._save_home(h, f"added activity {name} (no behaviour yet)")

    def remove_activity(self, name: str) -> dict[str, Any]:
        h = self.home()
        if name not in h["activities"]:
            raise KeyError(name)
        h["activities"].remove(name)
        return self._save_home(h, f"removed activity {name}")

    # ------------------------------------------------------------------ light groups
    def add_light(self, room: str, entity: str, name: str | None = None, zone: str = "room") -> RoomConfig:
        """Attach one light entity to a room as its own light group, owned by a zone (or the room itself)."""
        if not entity.startswith("light."):
            raise StructureError("only light.* entities can be added as lights")
        raw = self.room_raw(room)
        bulbs = raw.setdefault("bulbs", {})
        group = slug(name or entity.split(".", 1)[1])
        base, n = group, 2
        while group in bulbs:
            group, n = f"{base}_{n}", n + 1
        bulbs[group] = [entity]
        zones = (raw.get("presence") or {}).get("zones") or {}
        if zone != "room":
            if zone not in zones:
                raise StructureError(f"unknown zone {zone!r}")
            zones[zone].setdefault("lights", []).append(group)
        return self.save_room(raw, f"added light {entity} as {group}")

    def remove_light(self, room: str, group: str) -> RoomConfig:
        raw = self.room_raw(room)
        bulbs = raw.get("bulbs") or {}
        if group not in bulbs:
            raise StructureError(f"unknown light group {group!r}")
        del bulbs[group]
        for z in ((raw.get("presence") or {}).get("zones") or {}).values():
            z["lights"] = [l for l in (z.get("lights") or []) if l != group]
        return self.save_room(raw, f"removed light group {group}")

    def merge_lights(self, room: str, groups: list[str], new_name: str | None = None) -> RoomConfig:
        raw = self.room_raw(room)
        bulbs = raw.get("bulbs") or {}
        if len(groups) < 2 or any(g not in bulbs for g in groups):
            raise StructureError("pick at least two existing light groups")
        name = new_name or groups[0]
        if name in bulbs and name not in groups:
            raise StructureError(f"a light group called {name!r} already exists")
        entities = [e for g in groups for e in bulbs[g]]
        zones = (raw.get("presence") or {}).get("zones") or {}
        owner = next((z for z, zr in zones.items() if any(g in (zr.get("lights") or []) for g in groups)), None)
        for g in groups:
            bulbs.pop(g)
        bulbs[name] = entities
        for zr in zones.values():
            zr["lights"] = [l for l in (zr.get("lights") or []) if l not in groups]
        if owner:
            zones[owner]["lights"].append(name)
        return self.save_room(raw, f"merged {', '.join(groups)} into {name}")

    def split_lights(self, room: str, group: str) -> RoomConfig:
        raw = self.room_raw(room)
        bulbs = raw.get("bulbs") or {}
        if group not in bulbs or len(bulbs[group]) < 2:
            raise StructureError(f"{group!r} isn't a group of several lights")
        zones = (raw.get("presence") or {}).get("zones") or {}
        owner = next((z for z, zr in zones.items() if group in (zr.get("lights") or [])), None)
        new = {}
        for e in bulbs.pop(group):
            n = slug(e.split(".", 1)[-1])
            while n in bulbs or n in new:
                n += "_2"
            new[n] = [e]
        bulbs.update(new)
        if owner:
            zones[owner]["lights"] = [l for l in zones[owner]["lights"] if l != group] + list(new)
        return self.save_room(raw, f"split {group} into {', '.join(new)}")


# ---------------------------------------------------------------------- proposals from Home Assistant
def propose_room(area_name: str, entities: dict[str, list[str]], states: dict[str, dict[str, Any]],
                 existing_ids: set[str]) -> dict[str, Any]:
    """A filled-in room for an HA area: one light group per light, its presence sensors, lux sensor, TV, and a
    zone per radar zone sensor (entities whose id contains 'zone')."""
    rid = slug(area_name)
    while rid in existing_ids:
        rid += "_2"
    lights: dict[str, list[str]] = {}
    for e in entities.get("lights", []):
        n = slug(e.split(".", 1)[1])
        while n in lights:
            n += "_2"
        lights[n] = [e]
    pres = entities.get("presence", [])
    zone_sensors = [e for e in pres if re.search(r"zone[_ ]?\d", e)]
    room_sensors = [e for e in pres if e not in zone_sensors]
    zones = {f"zone_{i + 1}": {"presence": [e], "lights": [], "mode": "own", "signals": {}} for i, e in enumerate(zone_sensors)}

    def name(e: str) -> str:
        return ((states.get(e) or {}).get("attributes") or {}).get("friendly_name") or e

    return {
        "room": rid,
        "friendly_name": area_name,
        "lights": lights,
        "presence": room_sensors,
        "zones": zones,
        "lux_sensor": (entities.get("lux") or [None])[0],
        "media_sensor": (entities.get("media") or [None])[0],
        "names": {e: name(e) for kind in entities.values() for e in kind},
        "candidates": entities,
    }
