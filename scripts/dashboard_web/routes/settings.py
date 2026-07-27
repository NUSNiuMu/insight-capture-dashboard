"""SettingsRoutes HTTP handlers."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict

from aiohttp import web

from dashboard_web.context import DashboardContext
from dashboard_web.support import read_json_body


_ROSBAG_SYNC_FIELDS: Dict[str, type] = {
    "sync_rosbag_to_host": bool,
    "host_rosbag_sync_dir": str,
    "host_rosbag_sync_ssh_target": str,
}


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

    def _read_config_json(self, filename: str) -> Dict[str, object]:
        path = self.context.project_root / "config" / filename
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_config_fields(
        self, filename: str, fields: Dict[str, type], payload: Dict[str, object]
    ) -> Dict[str, object]:
        data = self._read_config_json(filename)
        for key, kind in fields.items():
            if key not in payload:
                continue
            raw_value = payload[key]
            try:
                value = kind(raw_value) if kind is not bool else bool(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"'{key}' must be a valid {kind.__name__}") from exc
            if kind in (float, int) and value <= 0:
                raise ValueError(f"'{key}' must be a positive number")
            data[key] = value
        path = self.context.project_root / "config" / filename
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {key: data.get(key) for key in fields}

    async def _handle_settings_rosbag_sync_get(self, _request: web.Request) -> web.Response:
        data = self._read_config_json("post_processing.json")
        return web.json_response(
            {"values": {key: data.get(key) for key in _ROSBAG_SYNC_FIELDS}, "restart_required": True}
        )

    async def _handle_settings_rosbag_sync_post(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        values = self._write_config_fields("post_processing.json", _ROSBAG_SYNC_FIELDS, payload)
        return web.json_response({"values": values, "restart_required": True})

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
