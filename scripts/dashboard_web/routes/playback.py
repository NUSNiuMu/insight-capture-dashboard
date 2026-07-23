"""PlaybackRoutes HTTP handlers."""

from __future__ import annotations

from aiohttp import web

from dashboard_web.support import bagplay_topic

from dashboard_web.context import DashboardContext


class PlaybackRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    def _on_playback_finished(self) -> None:
        self.context.node.set_playback_mode(False)
        self.context.node.clear_traces()

    async def _handle_playback_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        bag_name = str(body.get("bag_name", "")).strip()
        if not bag_name:
            return web.json_response({"error": "bag_name is required"}, status=400)
        self.context.node.set_playback_mode(True)
        self.context.node.clear_traces()
        # Remap every topic the dashboard displays onto its /bagplay/...
        # shadow so a still-connected live camera never blends with replay
        # (see bagplay_topic / _make_dashboard_image_callback).
        remap_topics = {camera.topic: bagplay_topic(camera.topic) for camera in self.context.node.cameras}
        remap_topics.update({pose.topic: bagplay_topic(pose.topic) for pose in self.context.node.poses})
        for camera in self.context.node.cameras:
            for tail in ("hand", "hand_keypoints"):
                hand_topic = f"/{camera.namespace}/camera/{tail}"
                remap_topics[hand_topic] = bagplay_topic(hand_topic)
        self.context.playback_manager.start(bag_name, self.context.recording_manager, remap_topics=remap_topics)
        return web.json_response({"status": "playing", "bag_name": bag_name})

    async def _handle_playback_stop(self, _request: web.Request) -> web.Response:
        self.context.playback_manager.stop()
        self.context.node.set_playback_mode(False)
        self.context.node.clear_traces()
        return web.json_response({"status": "idle"})

    async def _handle_playback_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.playback_manager.status())

    async def _handle_trajectory_clear(self, _request: web.Request) -> web.Response:
        self.context.node.clear_traces()
        return web.json_response({"ok": True})
