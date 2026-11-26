"""ROOM > PRESENCE > ZONE tracker, pattern learning store, OPNsense parsing, and controller-conflict guard."""

from datetime import datetime, timedelta

from luxador.ha_bridge import classify_context
from luxador.luxie import LuxieStore
from luxador.opnsense import NetHost, OpnsensePoller, parse_arp, parse_top
from luxador.parser import load_room_yaml
from luxador.presence import PresenceTracker

ROOM_YAML = """
room: test
presence:
  sensors: [binary_sensor.room_motion]
  clear_delay_seconds: 60
  zones:
    desk:
      presence: [binary_sensor.desk_zone]
      signals:
        laptop: { source: opnsense, host: "AA:BB:CC:00:11:22", active_kbps: 40 }
    sofa:
      presence: binary_sensor.sofa_zone
      signals:
        tv: { entity: media_player.tv, states: [playing] }
bulbs:
  lamp: light.lamp
rules:
  - "lamp :: state(working) :: 70% :: sun"
  - "lamp :: any :: 30% :: sun"
"""


class FakeNet:
    def __init__(self, hosts):
        self._p = OpnsensePoller()
        self._p.set_hosts(hosts)

    @property
    def ready(self):
        return True

    def lookup(self, host):
        return self._p.lookup(host)


def _states(desk=False, sofa=False, motion=False, tv="off"):
    on = lambda b: {"state": "on" if b else "off"}
    return {
        "binary_sensor.desk_zone": on(desk),
        "binary_sensor.sofa_zone": on(sofa),
        "binary_sensor.room_motion": on(motion),
        "media_player.tv": {"state": tv},
    }


def test_zone_config_shorthand_and_signals_parse():
    cfg = load_room_yaml(ROOM_YAML)
    assert cfg.presence.zones["sofa"].presence == ["binary_sensor.sofa_zone"]
    lap = cfg.presence.zones["desk"].signals[0]
    assert lap.source == "opnsense" and lap.active_kbps == 40
    assert cfg.rules[0].condition.kind == "state" and cfg.rules[0].condition.state_name == "working"


def test_signature_settles_and_clear_delay_holds():
    cfg = load_room_yaml(ROOM_YAML)
    net = FakeNet([NetHost(mac="aa:bb:cc:00:11:22", ip="10.0.0.5", hostname="kwn-laptop", online=True, kbps=120)])
    tr = PresenceTracker(settle_seconds=30)
    t0 = datetime(2026, 11, 26, 14, 0)

    u = tr.update(cfg, _states(), net, t0)
    assert u.snapshot.signature == "" and u.snapshot.live_signature == "desk.laptop"

    # Sit at the desk with the laptop busy: pattern only commits after settling
    u = tr.update(cfg, _states(desk=True), net, t0 + timedelta(seconds=5))
    assert u.snapshot.live_signature == "desk+desk.laptop"
    assert u.snapshot.signature == ""
    u = tr.update(cfg, _states(desk=True), net, t0 + timedelta(seconds=40))
    assert u.snapshot.signature == "desk+desk.laptop"
    assert u.closed_episode is not None and u.closed_episode[0] == ""
    # the new episode is dated from when the pattern began, not when it settled
    assert u.snapshot.signature_since == t0 + timedelta(seconds=5)

    # Radar drops for 20s: zone is held by clear delay, so no flicker in the signature
    u = tr.update(cfg, _states(desk=False), net, t0 + timedelta(seconds=60))
    assert u.snapshot.zones["desk"].held and u.snapshot.occupied
    assert u.snapshot.live_signature == "desk+desk.laptop"


def test_opnsense_unavailable_is_reported_not_faked():
    cfg = load_room_yaml(ROOM_YAML)
    tr = PresenceTracker()
    u = tr.update(cfg, _states(desk=True), None, datetime(2026, 11, 26, 9, 0))
    lap = next(s for s in u.snapshot.signals if s.name == "laptop")
    assert not lap.available and not lap.active


def test_parse_opnsense_payloads():
    hosts = parse_arp({"rows": [
        {"mac": "AA:BB:CC:00:11:22", "ip": "10.0.0.5", "hostname": "kwn-laptop.lan", "expired": False},
        {"mac": "11:22:33:44:55:66", "ip": "10.0.0.9", "hostname": "", "expired": True},
    ]})
    assert len(hosts) == 1 and hosts[0].mac == "aa:bb:cc:00:11:22"
    rates = parse_top({"lan": {"records": [
        {"address": "10.0.0.5", "rate_bits_in": 300000, "rate_bits_out": 100000},
    ]}}, "lan")
    assert rates["10.0.0.5"] == 400.0
    p = OpnsensePoller()
    p.set_hosts(hosts)
    assert p.lookup("kwn-laptop") is hosts[0] and p.lookup("10.0.0.5") is hosts[0]


def test_controller_conflicts_are_classified_and_flagged(tmp_path):
    assert classify_context({"user_id": "u1"})[0] == "user"
    assert classify_context({"parent_id": "p1"})[0] == "automation"
    assert classify_context({})[0] == "integration"

    store = LuxieStore(tmp_path / "t.db")
    store.record_foreign_write("light.lamp", "automation", "a", 50, 100, room="test")
    store.record_foreign_write("light.lamp", "automation", "a", 100, 40, room="test")
    store.record_foreign_write("light.other", "user", "u", 50, 60, room="test")
    lights = {l["entity_id"]: l for l in store.contested_lights("test")}
    assert lights["light.lamp"]["fighting"] is True
    assert lights["light.other"]["fighting"] is False