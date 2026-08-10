"""PlaybackRoutes HTTP handlers."""

from __future__ import annotations

from aiohttp import web

from dashboard_web.context import DashboardContext


class PlaybackRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_playback_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        bag_name = str(body.get("bag_name", "")).strip()
        if not bag_name:
            return web.json_response({"error": "bag_name is required"}, status=400)
        pose_by_name = {pose.name: pose for pose in self.context.node.poses}
        cameras = []
        for camera in self.context.node.cameras:
            pose = pose_by_name[camera.name]
            cameras.append(
                {
                    "name": camera.name,
                    "label": camera.label,
                    "topic": camera.topic,
                    "role": pose.teleop_role,
                    "rotation_deg": camera.rotation_deg,
                    "row": camera.row,
                    "column": camera.column,
                }
            )
        poses = [
            {
                "name": pose.name,
                "topic": pose.topic,
                "role": pose.teleop_role,
                "avatar_model": pose.avatar_model,
                "avatar_scale": pose.avatar_scale,
                "avatar_rotation_deg_xyz": list(pose.avatar_rotation_deg_xyz),
                "avatar_offset_xyz": list(pose.avatar_offset_xyz),
            }
            for pose in self.context.node.poses
        ]
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
        status = self.context.prepared_playback_manager.activate(bag_name)
        # Prepared media no longer consumes ROS playback topics. Gate the live
        # callbacks while it is visible so JPEG/WebRTC work cannot steal its
        # fixed 30 Hz browser frame budget.
        self.context.node.set_playback_mode(True)
        return web.json_response(status)

    async def _handle_playback_stop(self, _request: web.Request) -> web.Response:
        self.context.prepared_playback_manager.stop()
        self.context.node.set_playback_mode(False)
        self.context.node.clear_traces()
        return web.json_response({"status": "idle"})

    async def _handle_playback_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.prepared_playback_manager.status())

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
        self.context.prepared_playback_manager.stop()
        self.context.node.set_playback_mode(False)

    async def _handle_trajectory_clear(self, _request: web.Request) -> web.Response:
        self.context.node.clear_traces()
        return web.json_response({"ok": True})
