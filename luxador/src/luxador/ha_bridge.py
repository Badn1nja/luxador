"""Home Assistant WebSocket & REST Bridge with Context Attribution and Dry-Run support."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx
import websockets

from .models import RoleEvaluation

logger = logging.getLogger("luxador.ha_bridge")


class HABridge:
    """Connects Luxador to Home Assistant (or runs safely in offline/dry-run mode)."""

    def __init__(
        self,
        ha_url: str = "",
        ha_token: str = "",
        dry_run: bool = True,
        on_external_light_change: Callable[[str, int, int, str, str, str], None] | None = None,
        on_state_change: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        # Auto-detect Supervisor inside Home Assistant Apps (Add-ons)
        sup_token = os.environ.get("SUPERVISOR_TOKEN", "")
        if sup_token and not ha_url:
            self.ha_url = "http://supervisor/core"
            self.ha_token = sup_token
        else:
            self.ha_url = ha_url.rstrip("/")
            self.ha_token = ha_token

        self.dry_run = dry_run
        self.connected = False
        self.states: dict[str, dict[str, Any]] = {}
        self._own_context_ids: set[str] = set()
        self._on_external_light_change = on_external_light_change
        self._stop = False
        # Last payload we sent per entity: only resend when the target actually changes.
        # Luxador never re-asserts an unchanged target, so it can't get into a write war with another controller.
        self._last_sent: dict[str, tuple[str, str]] = {}
        # Recognising our own changes. HA's REST API won't take a caller-chosen context id, so we use
        # (1) the HA user id our token belongs to, and (2) a short window after each command where a
        # brightness matching what we just sent is ours.
        self.own_user_id: str | None = None
        self._recent_cmd: dict[str, tuple[float, int]] = {}  # entity -> (monotonic time, brightness %)
        self._on_state_change = on_state_change
        self.time_zone: str | None = None
        self.latitude: float | None = None
        self.longitude: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self.ha_url and self.ha_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        }

    def ws_url(self) -> str:
        if "supervisor/core" in self.ha_url:
            return "ws://supervisor/core/websocket"
        return self.ha_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    async def _ws_auth(self, ws: Any) -> None:
        auth_req = json.loads(await ws.recv())
        if auth_req.get("type") == "auth_required":
            await ws.send(json.dumps({"type": "auth", "access_token": self.ha_token}))
            auth_res = json.loads(await ws.recv())
            if auth_res.get("type") != "auth_ok":
                raise RuntimeError(f"HA WebSocket auth failed: {auth_res}")

    async def ws_command(self, payload: dict[str, Any], timeout: float = 60.0) -> Any:
        """One-shot WebSocket command (own connection), e.g. recorder/statistics_during_period."""
        async with websockets.connect(self.ws_url(), max_size=64 * 1024 * 1024) as ws:
            await self._ws_auth(ws)
            await ws.send(json.dumps({"id": 1, **payload}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if msg.get("id") == 1 and msg.get("type") == "result":
                    if not msg.get("success"):
                        raise RuntimeError(f"HA command {payload.get('type')} failed: {msg.get('error')}")
                    return msg.get("result")

    async def fetch_time_zone(self) -> str | None:
        if not self.configured:
            return None
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.ha_url}/api/config", headers=self._headers())
            resp.raise_for_status()
            cfg = resp.json()
            self.time_zone = cfg.get("time_zone")
            self.latitude = cfg.get("latitude")
            self.longitude = cfg.get("longitude")
        return self.time_zone

    async def fetch_history(
        self, entity_ids: list[str], start: datetime, end: datetime, chunk_hours: int = 24,
    ) -> list[dict[str, Any]]:
        """Recorder history for entities between start and end (UTC-aware), fetched a day at a time."""
        out: list[dict[str, Any]] = []
        if not self.configured or not entity_ids:
            return out
        async with httpx.AsyncClient(timeout=60.0) as client:
            t = start
            while t < end:
                t2 = min(end, t + timedelta(hours=chunk_hours))
                resp = await client.get(
                    f"{self.ha_url}/api/history/period/{t.isoformat()}",
                    headers=self._headers(),
                    params={"filter_entity_id": ",".join(entity_ids), "end_time": t2.isoformat(),
                            "significant_changes_only": "0"},
                )
                resp.raise_for_status()
                for series in resp.json() or []:
                    out.extend(series)
                t = t2
        return out

    async def fetch_statistics(self, statistic_ids: list[str], start: datetime, end: datetime) -> dict[str, list]:
        """Hourly long-term statistics (mean/min/max) for numeric sensors."""
        if not self.configured or not statistic_ids:
            return {}
        return await self.ws_command({
            "type": "recorder/statistics_during_period",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "statistic_ids": statistic_ids,
            "period": "hour",
            "types": ["mean", "min", "max"],
        }) or {}

    async def fetch_registries(self) -> tuple[list, list, list]:
        """HA's area, device and entity registries (where things physically are)."""
        areas = await self.ws_command({"type": "config/area_registry/list"}) or []
        devices = await self.ws_command({"type": "config/device_registry/list"}) or []
        entities = await self.ws_command({"type": "config/entity_registry/list"}) or []
        return areas, devices, entities

    def is_own_change(self, entity_id: str, ctx: dict[str, Any], new_bri: int) -> bool:
        ctx_id = ctx.get("id", "")
        parent_id = ctx.get("parent_id", "")
        if ctx_id in self._own_context_ids or parent_id in self._own_context_ids:
            return True
        if self.own_user_id and ctx.get("user_id") == self.own_user_id:
            return True
        recent = self._recent_cmd.get(entity_id)
        if recent and time.monotonic() - recent[0] < 15.0 and abs(recent[1] - new_bri) <= 2:
            return True
        return False

    def register_own_context(self, context_id: str) -> None:
        self._own_context_ids.add(context_id)
        if len(self._own_context_ids) > 500:
            # Keep memory bounded
            self._own_context_ids = set(list(self._own_context_ids)[-250:])

    async def fetch_initial_states(self) -> None:
        if not self.configured:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.ha_url}/api/states", headers=self._headers())
            resp.raise_for_status()
            for st in resp.json():
                self.states[st["entity_id"]] = st
        logger.info("Loaded %d HA entity states", len(self.states))

    async def run_ws_loop(self) -> None:
        """Maintain WebSocket subscription to HA state_changed events."""
        if not self.configured:
            logger.info("No HA URL/token configured; running in standalone sandbox mode.")
            return

        while not self._stop:
            try:
                await self.fetch_initial_states()
                try:
                    await self.fetch_time_zone()
                except Exception as exc:
                    logger.warning("Could not read HA time zone (%s); using system local time", exc)
                async with websockets.connect(self.ws_url()) as ws:
                    await self._ws_auth(ws)
                    await ws.send(json.dumps({"id": 1, "type": "auth/current_user"}))
                    await ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))
                    self.connected = True
                    self._last_sent.clear()  # fresh connection: allow one full re-send
                    while not self._stop:
                        msg = json.loads(await ws.recv())
                        if msg.get("id") == 1 and msg.get("type") == "result":
                            self.own_user_id = (msg.get("result") or {}).get("id")
                            logger.info("Luxador acts as HA user %s", self.own_user_id)
                        elif msg.get("type") == "event":
                            self._handle_state_event(msg.get("event", {}).get("data", {}))
            except Exception as exc:
                self.connected = False
                logger.warning("HA WebSocket disconnected (%s); retrying in 10s", exc)
                await asyncio.sleep(10)

    def _handle_state_event(self, data: dict[str, Any]) -> None:
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        old_state = data.get("old_state")
        if not entity_id or not new_state:
            return
        self.states[entity_id] = new_state
        if self._on_state_change:
            try:
                self._on_state_change(entity_id, new_state)
            except Exception as exc:  # never let capture break the event loop
                logger.warning("state capture failed for %s: %s", entity_id, exc)

        if not entity_id.startswith("light.") or not self._on_external_light_change:
            return

        ctx = new_state.get("context", {}) or {}
        ctx_id = ctx.get("id", "")
        new_bri = _extract_bri_pct(new_state)
        if self.is_own_change(entity_id, ctx, new_bri):
            return  # Initiated by El Luxador itself

        old_bri = _extract_bri_pct(old_state) if old_state else new_bri
        if new_bri != old_bri:
            kind, actor = classify_context(ctx)
            self._on_external_light_change(entity_id, old_bri, new_bri, kind, actor, ctx_id)

    async def apply_role(self, eval_role: RoleEvaluation, transition_seconds: int = 3) -> list[dict[str, Any]]:
        """Dispatch capability-safe light service calls (or dry-run log payloads)."""
        commands: list[dict[str, Any]] = []
        if eval_role.hands_off:
            return commands  # Standard §5: disabled / ignored lights are never sent anything
        if eval_role.transition_s is not None:
            transition_seconds = eval_role.transition_s
        for entity_id in eval_role.entities:
            if entity_id in eval_role.offline_entities:
                continue

            st = self.states.get(entity_id, {})
            attrs = st.get("attributes", {})
            supported = set(attrs.get("supported_color_modes", []))

            if not eval_role.on or eval_role.brightness_pct <= 0:
                service = "turn_off"
                payload: dict[str, Any] = {"entity_id": entity_id}
                if "onoff" not in supported and supported:
                    payload["transition"] = transition_seconds
            else:
                service = "turn_on"
                payload = {"entity_id": entity_id}
                # Only attach brightness/color if the entity supports more than pure on/off
                if supported and supported != {"onoff"}:
                    payload["brightness_pct"] = eval_role.brightness_pct
                    payload["transition"] = transition_seconds
                    if eval_role.color.mode == "kelvin" and eval_role.color.kelvin:
                        if "color_temp" in supported:
                            payload["color_temp_kelvin"] = eval_role.color.kelvin
                        elif "rgb" in supported or "xy" in supported:
                            if eval_role.color.rgb:
                                payload["rgb_color"] = list(eval_role.color.rgb)
                    elif eval_role.color.mode == "rgb" and eval_role.color.rgb:
                        if "rgb" in supported or "xy" in supported or "hs" in supported:
                            payload["rgb_color"] = list(eval_role.color.rgb)
                        elif "color_temp" in supported:
                            payload["color_temp_kelvin"] = 2200

            fingerprint = (service, json.dumps(payload, sort_keys=True))
            changed = self._last_sent.get(entity_id) != fingerprint
            commands.append({
                "domain": "light", "service": service, "data": payload,
                "dry_run": self.dry_run, "sent": changed,
            })
            if not changed:
                continue
            self._last_sent[entity_id] = fingerprint
            if not self.dry_run and self.configured:
                sent_bri = int(payload.get("brightness_pct", 100)) if service == "turn_on" else 0
                self._recent_cmd[entity_id] = (time.monotonic(), sent_bri)
                await self._call_service("light", service, payload)

        return commands

    async def write_logbook(self, name: str, message: str, entity_id: str | None = None) -> None:
        if self.dry_run or not self.configured:
            return
        payload: dict[str, Any] = {"name": name, "message": message}
        if entity_id:
            payload["entity_id"] = entity_id
        await self._call_service("logbook", "log", payload)

    async def _call_service(self, domain: str, service: str, payload: dict[str, Any]) -> None:
        ctx_id = uuid.uuid4().hex
        self.register_own_context(ctx_id)
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                resp = await client.post(
                    f"{self.ha_url}/api/services/{domain}/{service}",
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
            except Exception as exc:
                logger.error("Service call %s.%s failed: %s", domain, service, exc)


def classify_context(ctx: dict[str, Any]) -> tuple[str, str]:
    """Who changed a light, from HA's context.

    user        -> a person in the HA UI/app (context.user_id set)
    automation  -> an automation/script/integration reacting to something (context.parent_id set)
    integration -> no user, no parent: a physical switch/remote, or an add-on/integration calling directly
    """
    if ctx.get("user_id"):
        return "user", f"ha_user:{ctx.get('user_id')}"
    if ctx.get("parent_id"):
        return "automation", f"automation(parent={str(ctx.get('parent_id'))[:8]})"
    return "integration", "device_or_integration"


def _extract_bri_pct(state_obj: dict[str, Any] | None) -> int:
    if not state_obj:
        return 0
    if state_obj.get("state") != "on":
        return 0
    bri = (state_obj.get("attributes") or {}).get("brightness")
    if bri is None:
        return 100
    return int(round(float(bri) / 2.55))
