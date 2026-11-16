"""PlaybackRoutes HTTP handlers."""

from __future__ import annotations

import math

from aiohttp import web

from insight_capture.api.context import DashboardContext, capture_transition_lock


class PlaybackRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context
        self._transition = capture_transition_lock(context)

    async def _handle_playback_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        bag_name = str(body.get("bag_name", "")).strip()
        if not bag_name:
            return web.json_response({"error": "bag_name is required"}, status=400)
        if self._transition.locked():
            raise RuntimeError("A capture operation is still running.")
        async with self._transition:
            self._require_idle_capture()
            cameras, poses = self.context.playback_configuration()
            status = self.context.prepared_playback_manager.prepare(
                bag_name,
                self.context.recording_manager,
                cameras,
                poses,
            )
            return web.json_response(status)

    async def _handle_playback_activate(self, request: web.Request) -> web.Response:
        body = await request.json()
        bag_name = str(body.get("bag_name", "")).strip()
        if self._transition.locked():
            raise RuntimeError("A capture operation is still running.")
        async with self._transition:
            self._require_idle_capture()
            status = self.context.prepared_playback_manager.activate(bag_name)
            # Gate live display callbacks only after activation is admitted.
            self.context.node.set_playback_mode(True)
            return web.json_response(status)

    async def _handle_playback_stop(self, _request: web.Request) -> web.Response:
        if self._transition.locked():
            raise RuntimeError("A capture operation is still running.")
        async with self._transition:
            was_playing = self.context.prepared_playback_manager.status()["state"] == "playing"
            self.context.prepared_playback_manager.stop()
            self.context.node.set_playback_mode(False)
            # A duplicate Live request is not a command to clear a live path.
            if was_playing:
                self.context.node.clear_traces()
            return web.json_response({"status": "idle"})

    def _require_idle_capture(self) -> None:
        status = self.context.recording_manager.status()
        if (status.get("recording")
                or status.get("merge_state") in {"finalizing", "merging"}
                or (status.get("distributed") or {}).get("phase") == "starting"):
            raise RuntimeError("Cannot enter playback while capture is recording or saving.")

    async def _handle_playback_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.prepared_playback_manager.status())

    async def _handle_playback_browser_stats(self, request: web.Request) -> web.Response:
        body = await request.json()
        stats = {}
        for key in (
            "presented_fps",
            "current_time_s",
            "decoded_width",
            "decoded_height",
            "total_video_frames",
            "dropped_video_frames",
        ):
            try:
                value = float(body.get(key, 0))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                stats[key] = value
        self.context.prepared_playback_manager.record_browser_stats(stats)
        return web.Response(status=204)

    async def _handle_playback_artifact(self, request: web.Request) -> web.FileResponse:
        try:
            path = self.context.prepared_playback_manager.artifact_path(
                request.match_info["bag_name"], request.match_info["filename"]
            )
        except (ValueError, FileNotFoundError) as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        response = web.FileResponse(path)
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return response

    async def _on_shutdown(self, _app: web.Application) -> None:
        self.context.prepared_playback_manager.shutdown()
        self.context.node.set_playback_mode(False)

    async def _handle_trajectory_clear(self, _request: web.Request) -> web.Response:
        self.context.node.clear_traces()
        return web.json_response({"ok": True})
