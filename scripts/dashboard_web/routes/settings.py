"""SettingsRoutes HTTP handlers."""

from __future__ import annotations

import os
import threading
import time

from aiohttp import web

from dashboard_web.context import DashboardContext
from dashboard_web.support import read_json_body


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

    async def _handle_settings_stick_figure(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        if "enabled" not in payload:
            raise ValueError("Field 'enabled' is required.")
        self.context.node.stick_figure_mode = bool(payload.get("enabled"))
        return web.json_response(self.context.node.build_settings_payload())

    async def _handle_settings_get(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.node.build_settings_payload())

    async def _handle_settings_avatar_model(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        name = str(payload.get("name", "")).strip()
        model = str(payload.get("model", "")).strip()
        if not name or not model:
            raise ValueError("Fields 'name' and 'model' are required.")
        self.context.node.set_pose_avatar_model(name, model)
        return web.json_response(self.context.node.build_settings_payload())

    async def _handle_settings_gripper_tracking(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        name = str(payload.get("name", "")).strip()
        if not name or "enabled" not in payload:
            raise ValueError("Fields 'name' and 'enabled' are required.")
        self.context.node.set_gripper_tracking_enabled(name, bool(payload.get("enabled")))
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
