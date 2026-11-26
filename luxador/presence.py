"""ROOM > PRESENCE > ZONE engine.

Reads every presence sensor and signal attached to a room and its zones, applies the clear-delay
hold, and distils the result into a *signature*: the sorted set of what is true right now
(e.g. ``desk+desk.laptop``). A signature only becomes the room's committed pattern after it has been
stable for ``settle_seconds``, so a 5-second sensor flicker or a TV blip never counts as a state.

States themselves are never configured here. Luxie records how long each committed signature lasts,
and you name a recurring one once (see LuxieStore.pattern_summary / set_pattern_label).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .models import RoomConfig, SignalSpec

ROOM_SCOPE = "room"
_ACTIVE_WORDS = {"on", "home", "playing", "open", "true", "detected", "occupied"}


class NetLookup(Protocol):
    """Anything that can answer 'is this LAN host online and how busy is it' (the OPNsense poller)."""

    @property
    def ready(self) -> bool: ...

    def lookup(self, host: str) -> Any: ...


@dataclass
class SignalReading:
    scope: str  # zone name, or "room"
    name: str
    source: str
    active: bool
    value: Any = None
    available: bool = True
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.scope}.{self.name}"


@dataclass
class ZoneSnapshot:
    name: str
    raw: bool  # a presence sensor says yes right now
    occupied: bool  # raw, or held by the clear delay
    held: bool
    sensors: dict[str, str] = field(default_factory=dict)


@dataclass
class PresenceSnapshot:
    room: str
    ts: datetime
    occupied: bool
    room_held: bool
    zones: dict[str, ZoneSnapshot]
    signals: list[SignalReading]
    live_signature: str
    signature: str  # committed (settled) signature
    signature_since: datetime
    pending_signature: str | None
    idle_minutes: float
    recorded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "room": self.room,
            "ts": self.ts.isoformat(),
            "occupied": self.occupied,
            "room_held": self.room_held,
            "zones": {
                z.name: {"raw": z.raw, "occupied": z.occupied, "held": z.held, "sensors": z.sensors}
                for z in self.zones.values()
            },
            "signals": [
                {
                    "key": s.key, "scope": s.scope, "name": s.name, "source": s.source,
                    "active": s.active, "value": s.value, "available": s.available, "detail": s.detail,
                }
                for s in self.signals
            ],
            "live_signature": self.live_signature,
            "signature": self.signature,
            "signature_since": self.signature_since.isoformat(),
            "pending_signature": self.pending_signature,
            "idle_minutes": round(self.idle_minutes, 1),
            "recorded": self.recorded,
        }


def _ha_active(raw: Any) -> bool:
    return str(raw).strip().lower() in _ACTIVE_WORDS


def read_signal(spec: SignalSpec, scope: str, ha_states: dict[str, dict[str, Any]], net: NetLookup | None) -> SignalReading:
    """Evaluate one configured signal against HA state cache or the OPNsense host table."""
    if spec.source == "opnsense":
        if net is None or not net.ready:
            return SignalReading(scope, spec.name, "opnsense", False, None, available=False,
                                 detail="OPNsense not connected")
        host = net.lookup(spec.host or "")
        if host is None or not getattr(host, "online", False):
            return SignalReading(scope, spec.name, "opnsense", False, 0.0, detail="offline / not in ARP")
        kbps = float(getattr(host, "kbps", 0.0) or 0.0)
        active = kbps >= spec.active_kbps if spec.active_kbps is not None else True
        return SignalReading(scope, spec.name, "opnsense", active, round(kbps, 1),
                             detail=f"online, {kbps:.0f} kbps")

    st = ha_states.get(spec.entity or "")
    if not st:
        return SignalReading(scope, spec.name, "ha", False, None, available=False, detail="entity not found")
    raw = st.get("state")
    if raw in (None, "unavailable", "unknown"):
        return SignalReading(scope, spec.name, "ha", False, raw, available=False, detail=str(raw))
    if spec.states:
        active = str(raw).lower() in {s.lower() for s in spec.states}
        return SignalReading(scope, spec.name, "ha", active, raw)
    if spec.above is not None or spec.below is not None:
        try:
            num = float(raw)
        except (TypeError, ValueError):
            return SignalReading(scope, spec.name, "ha", False, raw, available=False, detail="not numeric")
        active = (spec.above is None or num > spec.above) and (spec.below is None or num < spec.below)
        return SignalReading(scope, spec.name, "ha", active, num)
    return SignalReading(scope, spec.name, "ha", _ha_active(raw), raw)


def build_signature(occupied: bool, zones: dict[str, ZoneSnapshot], signals: list[SignalReading]) -> str:
    """Sorted '+'-joined tokens. Empty string = nobody here and nothing active."""
    tokens: set[str] = set()
    for z in zones.values():
        if z.occupied:
            tokens.add(z.name)
    for s in signals:
        if s.active:
            tokens.add(s.name if s.scope == ROOM_SCOPE else s.key)
    if occupied and not any(z.occupied for z in zones.values()):
        tokens.add(ROOM_SCOPE)
    return "+".join(sorted(tokens))


@dataclass
class _RoomTrack:
    zone_last_seen: dict[str, datetime] = field(default_factory=dict)
    room_last_seen: datetime | None = None
    last_raw_vector: tuple | None = None
    last_change: datetime | None = None
    committed: str = ""
    committed_since: datetime | None = None
    pending: str | None = None
    pending_since: datetime | None = None
    last_signals: dict[str, tuple[bool, Any]] = field(default_factory=dict)


@dataclass
class PresenceUpdate:
    snapshot: PresenceSnapshot
    signal_changes: list[SignalReading]
    closed_episode: tuple[str, datetime, datetime] | None  # (signature, start, end)


class PresenceTracker:
    """In-memory per-room presence state. Persisting is the caller's job (only real HA data is recorded)."""

    def __init__(self, settle_seconds: float = 45.0) -> None:
        self.settle_seconds = settle_seconds
        self._rooms: dict[str, _RoomTrack] = {}

    def update(
        self,
        cfg: RoomConfig,
        ha_states: dict[str, dict[str, Any]],
        net: NetLookup | None,
        now: datetime,
        zone_overrides: dict[str, bool] | None = None,
        occupied_override: bool | None = None,
        hold: bool = True,
        settle_seconds: float | None = None,
    ) -> PresenceUpdate:
        settle = self.settle_seconds if settle_seconds is None else settle_seconds
        tr = self._rooms.setdefault(cfg.room, _RoomTrack(committed_since=now, last_change=now))
        delay = max(0, cfg.presence.clear_delay_seconds) if hold else 0

        zones: dict[str, ZoneSnapshot] = {}
        for zname, zcfg in cfg.presence.zones.items():
            sensors = {e: str(ha_states.get(e, {}).get("state", "missing")) for e in zcfg.presence}
            raw = any(_ha_active(v) for v in sensors.values())
            if zone_overrides is not None and zname in zone_overrides:
                raw = bool(zone_overrides[zname])
            if raw:
                tr.zone_last_seen[zname] = now
            last = tr.zone_last_seen.get(zname)
            held = (not raw) and last is not None and (now - last).total_seconds() < delay
            zones[zname] = ZoneSnapshot(zname, raw, raw or held, held, sensors)

        room_raw = any(_ha_active(ha_states.get(e, {}).get("state")) for e in cfg.presence.sensors)
        room_raw = room_raw or any(z.raw for z in zones.values())
        if occupied_override is not None:
            room_raw = occupied_override
        if room_raw:
            tr.room_last_seen = now
        room_held = (not room_raw) and tr.room_last_seen is not None and (
            (now - tr.room_last_seen).total_seconds() < delay
        )
        occupied = room_raw or room_held

        signals: list[SignalReading] = [read_signal(s, ROOM_SCOPE, ha_states, net) for s in cfg.presence.signals]
        for zname, zcfg in cfg.presence.zones.items():
            signals.extend(read_signal(s, zname, ha_states, net) for s in zcfg.signals)

        # Idle = minutes since any raw presence or signal value last changed (radar holds 'on' while still,
        # so this measures stillness, not absence)
        raw_vector = (room_raw, tuple(z.raw for z in zones.values()), tuple((s.key, s.active) for s in signals))
        if raw_vector != tr.last_raw_vector:
            tr.last_raw_vector = raw_vector
            tr.last_change = now
        idle_minutes = max(0.0, (now - (tr.last_change or now)).total_seconds() / 60.0)

        changes: list[SignalReading] = []
        for s in signals:
            prev = tr.last_signals.get(s.key)
            cur = (s.active, s.available)
            if prev != cur:
                changes.append(s)
                tr.last_signals[s.key] = cur

        live_sig = build_signature(occupied, zones, signals)
        closed: tuple[str, datetime, datetime] | None = None
        if live_sig == tr.committed:
            tr.pending, tr.pending_since = None, None
        else:
            if tr.pending != live_sig:
                tr.pending, tr.pending_since = live_sig, now
            if tr.pending_since is not None and (now - tr.pending_since).total_seconds() >= settle:
                # Episode boundary is when the new pattern actually started, not when it settled
                start_prev = tr.committed_since or tr.pending_since
                closed = (tr.committed, start_prev, tr.pending_since)
                tr.committed, tr.committed_since = live_sig, tr.pending_since
                tr.pending, tr.pending_since = None, None

        snap = PresenceSnapshot(
            room=cfg.room,
            ts=now,
            occupied=occupied,
            room_held=room_held,
            zones=zones,
            signals=signals,
            live_signature=live_sig,
            signature=tr.committed,
            signature_since=tr.committed_since or now,
            pending_signature=tr.pending,
            idle_minutes=idle_minutes,
        )
        return PresenceUpdate(snap, changes, closed)

    def current_episode(self, room: str) -> tuple[str, datetime] | None:
        tr = self._rooms.get(room)
        if tr is None or tr.committed_since is None:
            return None
        return tr.committed, tr.committed_since
