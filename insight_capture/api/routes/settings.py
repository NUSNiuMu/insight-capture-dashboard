"""SettingsRoutes HTTP handlers."""

from __future__ import annotations

import asyncio
import os

from aiohttp import web

from insight_capture.api.context import DashboardContext
from insight_capture.api.support import read_json_body
from insight_capture.core.localization_settings import save_gripper_mask_height_ratio


class SettingsRoutes:
    def __init__(self, context: DashboardContext, recording) -> None:
        self.context = context
        self.recording = recording
        self._restart_task = None

    async def _handle_settings_get(self, _request: web.Request) -> web.Response:
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
        if self._restart_task is not None and not self._restart_task.done():
            return web.json_response({
                "error": "backend_restarting", "message": "后端正在重启，请稍后重试。",
            }, status=409)
        if self.recording.busy:
            return self._restart_conflict()
        # Keep capture admission closed from this check until process exit.
        # A client can otherwise start during the response's brief grace period.
        await self.recording._transition.acquire()
        scheduled = False
        try:
            status = await asyncio.to_thread(self.context.recording_manager.status)
            if (status.get("recording")
                    or status.get("merge_state") in {"finalizing", "merging"}
                    or (status.get("distributed") or {}).get("phase") == "starting"):
                return self._restart_conflict()
            self.context.node.get_logger().info("Settings: restarting idle backend.")
            self._restart_task = asyncio.create_task(self._delayed_exit())
            scheduled = True
            return web.json_response({"ok": True, "message": "Restarting backend..."})
        finally:
            if not scheduled:
                self.recording._transition.release()

    @staticmethod
    def _restart_conflict() -> web.Response:
        return web.json_response({
            "error": "capture_busy",
            "message": "正在准备、录制或保存，请完成后再重启。",
        }, status=409)

    async def _delayed_exit(self) -> None:
        try:
            await asyncio.sleep(0.5)
            os._exit(0)
        finally:
            self.recording._transition.release()
