"""SettingsRoutes HTTP handlers."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request

from aiohttp import web

from insight_capture.api.context import DashboardContext
from insight_capture.api.support import read_json_body
from insight_capture.core.localization_settings import save_gripper_mask_height_ratio


class SettingsRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context
        self.voice_control_url = os.environ.get(
            "INSIGHT_VOICE_CONTROL_URL",
            "http://127.0.0.1:8770",
        ).rstrip("/")

    def _voice_control_request_sync(
        self,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.voice_control_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read()).get("error")
            except Exception:  # noqa: BLE001 - retain the upstream status below
                error = None
            raise RuntimeError(error or f"voice service returned HTTP {exc.code}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"voice service is unavailable: {exc}") from exc
        if not isinstance(result, dict):
            raise RuntimeError("voice service returned an invalid response")
        return result

    async def _voice_control_request(
        self,
        path: str,
        *,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._voice_control_request_sync,
            path,
            payload=payload,
        )

    async def _settings_payload(self) -> dict[str, object]:
        payload = self.context.node.build_settings_payload()
        try:
            payload["voice_audio"] = await self._voice_control_request("/v1/audio")
        except RuntimeError as exc:
            payload["voice_audio"] = {
                "available": False,
                "error": str(exc),
            }
        return payload

    async def _handle_settings_hand_overlay(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        name = str(payload.get("name", "")).strip()
        if not name or "enabled" not in payload:
            raise ValueError("Fields 'name' and 'enabled' are required.")
        self.context.node.set_hand_overlay_enabled(name, bool(payload.get("enabled")))
        return web.json_response(await self._settings_payload())

    async def _handle_settings_get(self, _request: web.Request) -> web.Response:
        return web.json_response(await self._settings_payload())

    async def _handle_settings_gripper_tracking(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        name = str(payload.get("name", "")).strip()
        if not name or "enabled" not in payload:
            raise ValueError("Fields 'name' and 'enabled' are required.")
        self.context.node.set_gripper_tracking_enabled(name, bool(payload.get("enabled")))
        return web.json_response(await self._settings_payload())

    async def _handle_settings_insight3_gripper_mask(
        self, request: web.Request
    ) -> web.Response:
        payload = await read_json_body(request)
        if "value" not in payload:
            raise ValueError("Field 'value' is required.")
        ratio = save_gripper_mask_height_ratio(
            self.context.node.post_processing_config_path,
            payload["value"],
        )
        self.context.node.get_logger().info(
            f"Settings: Insight3 gripper mask height ratio saved as {ratio}"
        )
        return web.json_response(await self._settings_payload())

    async def _handle_settings_voice_volume(
        self, request: web.Request
    ) -> web.Response:
        payload = await read_json_body(request)
        if "volume_percent" not in payload:
            raise ValueError("Field 'volume_percent' is required.")
        try:
            voice_audio = await self._voice_control_request(
                "/v1/audio/volume",
                payload={"volume_percent": payload["volume_percent"]},
            )
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        return web.json_response({"voice_audio": voice_audio})

    async def _handle_settings_voice_sample(
        self, _request: web.Request
    ) -> web.Response:
        try:
            voice_audio = await self._voice_control_request("/v1/audio/sample", payload={})
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        return web.json_response({"voice_audio": voice_audio})

    async def _handle_settings_restart(self, _request: web.Request) -> web.Response:
        self.context.node.get_logger().info(
            "Settings: restart requested from the web UI; exiting so "
            "'restart: unless-stopped' brings the backend back up with reloaded config."
        )

        def _delayed_exit() -> None:
            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=_delayed_exit, daemon=True).start()
        return web.json_response({"ok": True, "message": "Restarting backend..."})
