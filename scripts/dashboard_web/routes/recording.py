"""RecordingRoutes HTTP handlers."""

from __future__ import annotations

import asyncio
import json
import shutil

from aiohttp import web

from post_processing_core.integrity import analyze_bag
from post_processing import list_rosbags
from dashboard_web.support import read_disk_space, read_json_body, read_system_load

from dashboard_web.context import DashboardContext


class RecordingRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_recording_status(self, _request: web.Request) -> web.Response:
        payload = self.context.recording_manager.status()
        payload["system_load"] = read_system_load()
        payload["disk_space"] = read_disk_space(self.context.recording_manager.rosbag_root)
        payload["gesture_recording"] = self.context.node.gesture_recording_status(payload)
        payload["voice_recording"] = self.context.node.voice_recording_status(payload)
        return web.json_response(payload)

    async def _handle_recording_topics(self, _request: web.Request) -> web.Response:
        loop = asyncio.get_running_loop()
        catalog = await loop.run_in_executor(
            None,
            lambda: self.context.recording_manager.current_topic_catalog(refresh=True),
        )
        return web.json_response(catalog)

    async def _handle_recording_start(self, request: web.Request) -> web.Response:
        payload = {}
        if request.can_read_body:
            try:
                payload = await request.json()
            except json.JSONDecodeError as exc:
                return web.json_response({"error": f"Invalid JSON body: {exc}"}, status=400)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return web.json_response({"error": "Request body must be a JSON object."}, status=400)
        topics = payload.get("topics")
        if "topics" in payload and not isinstance(topics, list):
            return web.json_response({"error": "Field 'topics' must be a list."}, status=400)
        bag_name = str(payload.get("bag_name", "")).strip() or None
        status = self.context.recording_manager.start(topics=topics, bag_name=bag_name)
        return web.json_response(status)

    async def _handle_recording_stop(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.recording_manager.stop())

    async def _handle_rosbag_list(self, _request: web.Request) -> web.Response:
        loop = asyncio.get_event_loop()
        bags = await loop.run_in_executor(
            None, list_rosbags, self.context.recording_manager.rosbag_root, self.context.results_root
        )
        return web.json_response(
            {
                "type": "rosbag_list",
                "rosbag_root": str(self.context.recording_manager.rosbag_root),
                "results_root": str(self.context.results_root),
                "bags": bags,
            }
        )

    async def _handle_rosbag_delete(self, request: web.Request) -> web.Response:
        bag_name = request.match_info.get("bag_name", "").strip()
        if not bag_name or "/" in bag_name or bag_name in (".", ".."):
            return web.json_response({"error": "Invalid bag name."}, status=400)
        bag_path = (self.context.recording_manager.rosbag_root / bag_name).resolve()
        if not bag_path.is_relative_to(self.context.recording_manager.rosbag_root.resolve()):
            return web.json_response({"error": "Access denied."}, status=403)
        if not bag_path.exists():
            return web.json_response({"error": "Bag not found."}, status=404)
        shutil.rmtree(bag_path)
        return web.json_response({"status": "deleted", "bag_name": bag_name})

    async def _handle_integrity_run(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        bag_name = str(payload.get("bag_name", "")).strip()
        if not bag_name or "/" in bag_name or bag_name in (".", ".."):
            return web.json_response({"error": "Invalid bag name."}, status=400)
        bag_path = (self.context.recording_manager.rosbag_root / bag_name).resolve()
        if not bag_path.is_relative_to(self.context.recording_manager.rosbag_root.resolve()):
            return web.json_response({"error": "Access denied."}, status=403)
        if not bag_path.exists():
            return web.json_response({"error": "Bag not found."}, status=404)
        loop = asyncio.get_event_loop()
        try:
            # SQLite aggregates provide each topic's count and active time
            # window without reading any frame payloads.
            report = await loop.run_in_executor(
                None, lambda: analyze_bag(bag_path, deep=False)
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=422)
        # Persisted next to scores/optimized so list_rosbags can surface a
        # per-bag integrity badge without re-scanning gigabytes per listing.
        integrity_dir = self.context.results_root / "integrity"
        integrity_dir.mkdir(parents=True, exist_ok=True)
        (integrity_dir / f"{bag_name}.json").write_text(json.dumps(report, indent=2))
        return web.json_response({"type": "integrity_report", **report})

    async def _handle_scoring_run(self, request: web.Request) -> web.Response:
        if request.can_read_body:
            try:
                body = await request.json()
            except json.JSONDecodeError as exc:
                return web.json_response({"error": f"Invalid JSON: {exc}"}, status=400)
        else:
            body = {}
        if not isinstance(body, dict):
            body = {}
        bag_name = str(body.get("bag_name", "")).strip()
        if not bag_name:
            return web.json_response({"error": "bag_name is required"}, status=400)
        topic = str(body.get("topic", "")).strip()
        started = self.context.scoring_manager.run(bag_name, topic)
        if not started:
            return web.json_response({"error": "A scoring job is already running."}, status=409)
        return web.json_response({"status": "started", "bag_name": bag_name})

    async def _handle_scoring_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.scoring_manager.status)
