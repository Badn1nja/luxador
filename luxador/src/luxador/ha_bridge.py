"""Home Assistant WebSocket & REST Bridge with Context Attribution and Dry-Run support."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
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

    @property
    def configured(self) -> bool:
        return bool(self.ha_url and self.ha_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        }

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

        ws_url = self.ha_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        if "supervisor/core" in self.ha_url:
            ws_url = "ws://supervisor/core/websocket"

        while not self._stop:
            try:
                await self.fetch_initial_states()
                async with websockets.connect(ws_url) as ws:
                    auth_req = json.loads(await ws.recv())
                    if auth_req.get("type") == "auth_required":
                        await ws.send(json.dumps({"type": "auth", "access_token": self.ha_token}))
                        auth_res = json.loads(await ws.recv())
                        if auth_res.get("type") != "auth_ok":
                            raise RuntimeError(f"HA WebSocket auth failed: {auth_res}")

                    self.connected = True
                    self._last_sent.clear()  # fresh connection: allow one full re-send
                    await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
                    while not self._stop:
                        msg = json.loads(await ws.recv())
                        if msg.get("type") == "event":
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

        if not entity_id.startswith("light.") or not self._on_external_light_change:
            return

        ctx = new_state.get("context", {}) or {}
        ctx_id = ctx.get("id", "")
        parent_id = ctx.get("parent_id", "")
        if ctx_id in self._own_context_ids or parent_id in self._own_context_ids:
            return  # Initiated by El Luxador itself

        new_bri = _extract_bri_pct(new_state)
        old_bri = _extract_bri_pct(old_state) if old_state else new_bri
        if new_bri != old_bri:
            kind, actor = classify_context(ctx)
            self._on_external_light_change(entity_id, old_bri, new_bri, kind, actor, ctx_id)

    async def apply_role(self, eval_role: RoleEvaluation, transition_seconds: int = 3) -> list[dict[str, Any]]:
        """Dispatch capability-safe light service calls (or dry-run log payloads)."""
        commands: list[dict[str, Any]] = []
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
