"""SettingsRoutes HTTP handlers."""

from __future__ import annotations

import os
import threading
import time

from aiohttp import web

from insight_capture.web.context import DashboardContext
from insight_capture.web.support import read_json_body
from insight_capture.common.localization_settings import save_gripper_mask_height_ratio


class SettingsRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_settings_hand_overlay(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        name = str(payload.get("name", "")).strip()
        if not name or "enabled" not in payload:
            raise ValueError("Fields 'name' and 'enabled' are required.")
        self.context.node.set_hand_overlay_enabled(name, bool(payload.get("enabled")))
        return web.json_response(self.context.node.build_settings_payload())

    async def _handle_settings_get(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.node.build_settings_payload())

    async def _handle_settings_gripper_tracking(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        name = str(payload.get("name", "")).strip()
        if not name or "enabled" not in payload:
            raise ValueError("Fields 'name' and 'enabled' are required.")
        self.context.node.set_gripper_tracking_enabled(name, bool(payload.get("enabled")))
        return web.json_response(self.context.node.build_settings_payload())

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
        return web.json_response(self.context.node.build_settings_payload())

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
