"""OPNsense poller: which LAN hosts are online (ARP) and how much traffic each is moving (top talkers).

Uses the OPNsense REST API with an API key/secret (System > Access > Users > API keys).
The key's user needs the 'Diagnostics: ARP Table' and 'Reporting: Traffic' privileges.

Caveat: ARP entries linger for minutes after a device leaves, so 'online' is slow to drop.
Traffic rate (`active_kbps` on a signal) is the much better 'actually in use' signal.

NOTE: endpoint paths/field names are written defensively from the OPNsense API docs and have NOT been
tested against a live box yet. `last_error` and /api/net/status show what came back.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("lantern.opnsense")

ARP_PATHS = ("/api/diagnostics/interface/get_arp", "/api/diagnostics/interface/getArp")
TOP_PATH = "/api/diagnostics/traffic/top/{iface}"


@dataclass
class NetHost:
    mac: str = ""
    ip: str = ""
    hostname: str = ""
    online: bool = False
    kbps: float = 0.0
    last_seen: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"mac": self.mac, "ip": self.ip, "hostname": self.hostname, "online": self.online,
                "kbps": round(self.kbps, 1), "last_seen": self.last_seen}


def parse_arp(payload: Any) -> list[NetHost]:
    """ARP endpoint returns a list of rows (older) or {"rows": [...]} (newer)."""
    rows = payload.get("rows", []) if isinstance(payload, dict) else payload
    hosts: list[NetHost] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if r.get("expired") in (True, "true", 1):
            continue
        mac = str(r.get("mac", "")).lower()
        ip = str(r.get("ip", ""))
        if not mac and not ip:
            continue
        hosts.append(NetHost(mac=mac, ip=ip, hostname=str(r.get("hostname", "") or ""), online=True))
    return hosts


def parse_top(payload: Any, iface: str) -> dict[str, float]:
    """Top-talkers endpoint: {iface: {"records": [{address, rate_bits_in, rate_bits_out | rate_bits}]}} -> {ip: kbps}."""
    if not isinstance(payload, dict):
        return {}
    block = payload.get(iface) or next((v for v in payload.values() if isinstance(v, dict) and "records" in v), {})
    out: dict[str, float] = {}
    for rec in (block or {}).get("records", []) or []:
        addr = str(rec.get("address", ""))
        if not addr:
            continue
        if "rate_bits" in rec:
            bits = float(rec.get("rate_bits") or 0)
        else:
            bits = float(rec.get("rate_bits_in") or 0) + float(rec.get("rate_bits_out") or 0)
        out[addr] = out.get(addr, 0.0) + bits / 1000.0
    return out


class OpnsensePoller:
    def __init__(
        self,
        url: str = "",
        api_key: str = "",
        api_secret: str = "",
        verify_ssl: bool = True,
        interface: str = "lan",
        poll_seconds: float = 15.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.verify_ssl = verify_ssl
        self.interface = interface
        self.poll_seconds = max(5.0, poll_seconds)
        self.hosts: list[NetHost] = []
        self._index: dict[str, NetHost] = {}
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self._stop = False

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key and self.api_secret)

    @property
    def ready(self) -> bool:
        if self.last_ok is None:
            return False
        return (datetime.now(timezone.utc) - self.last_ok).total_seconds() < self.poll_seconds * 4

    def lookup(self, host: str) -> NetHost | None:
        return self._index.get(host.strip().lower())

    def set_hosts(self, hosts: list[NetHost]) -> None:
        self.hosts = hosts
        idx: dict[str, NetHost] = {}
        for h in hosts:
            for k in (h.mac, h.ip, h.hostname, h.hostname.split(".")[0] if h.hostname else ""):
                if k:
                    idx[k.lower()] = h
        self._index = idx

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "ready": self.ready,
            "last_ok": self.last_ok.isoformat() if self.last_ok else None,
            "last_error": self.last_error,
            "host_count": len(self.hosts),
        }

    async def poll_once(self, client: httpx.AsyncClient) -> None:
        arp_payload: Any = None
        last_exc: Exception | None = None
        for path in ARP_PATHS:
            try:
                resp = await client.get(self.url + path)
                resp.raise_for_status()
                arp_payload = resp.json()
                break
            except Exception as exc:  # try the other spelling
                last_exc = exc
        if arp_payload is None:
            raise RuntimeError(f"ARP fetch failed: {last_exc}")
        hosts = parse_arp(arp_payload)

        rates: dict[str, float] = {}
        try:
            resp = await client.get(self.url + TOP_PATH.format(iface=self.interface))
            resp.raise_for_status()
            rates = parse_top(resp.json(), self.interface)
        except Exception as exc:
            self.last_error = f"traffic/top unavailable ({exc}); online-only mode"

        now_s = datetime.now(timezone.utc).isoformat()
        for h in hosts:
            h.kbps = rates.get(h.ip, 0.0)
            h.last_seen = now_s
        self.set_hosts(hosts)
        self.last_ok = datetime.now(timezone.utc)
        if not rates:
            return
        self.last_error = None

    async def run(self) -> None:
        if not self.configured:
            logger.info("OPNsense not configured; network signals will show as unavailable.")
            return
        async with httpx.AsyncClient(
            auth=(self.api_key, self.api_secret), verify=self.verify_ssl, timeout=8.0
        ) as client:
            while not self._stop:
                try:
                    await self.poll_once(client)
                except Exception as exc:
                    self.last_error = str(exc)
                    logger.warning("OPNsense poll failed: %s", exc)
                await asyncio.sleep(self.poll_seconds)
