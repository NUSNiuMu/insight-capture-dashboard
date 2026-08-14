"""Fixed-station capture quality-check routes."""

from pathlib import Path

from aiohttp import web

from ..context import DashboardContext


class CaptureCheckRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    def _latest_bag_name(self) -> str | None:
        output_path = str(self.context.recording_manager.status().get("output_path") or "")
        return Path(output_path).name or None

    def _recording_guard(self) -> dict | None:
        if not self.context.recording_manager.status().get("recording"):
            return None
        return {
            "type": "capture_check_result",
            "state": "not_ready",
            "reasons": ["stop the active recording before running a station check"],
        }

    async def _handle_status(self, _request: web.Request) -> web.Response:
        return web.json_response(
            self.context.node.capture_check_status(bag_name=self._latest_bag_name())
        )

    async def _handle_reference(self, _request: web.Request) -> web.Response:
        guarded = self._recording_guard()
        if guarded is not None:
            return web.json_response(guarded)
        return web.json_response(self.context.node.set_capture_check_reference())

    async def _handle_run(self, _request: web.Request) -> web.Response:
        guarded = self._recording_guard()
        if guarded is not None:
            return web.json_response(guarded)
        return web.json_response(
            self.context.node.run_capture_check(bag_name=self._latest_bag_name())
        )
