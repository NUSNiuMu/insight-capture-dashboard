"""RecordingRoutes HTTP handlers."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from aiohttp import web

from insight_capture.postprocess.bags.integrity import analyze_bag
from insight_capture.postprocess.bags import BagLibrary, list_rosbag_locations
from insight_capture.api.support import read_disk_space, read_json_body, read_system_load

from insight_capture.api.context import DashboardContext


class RecordingRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    def _bag_library(self) -> BagLibrary:
        library = getattr(self.context, "bag_library", None)
        if library is not None:
            return library
        manager = self.context.recording_manager
        return BagLibrary(manager.storage_browse_roots, lambda: manager.rosbag_root)

    async def _handle_recording_status(self, _request: web.Request) -> web.Response:
        payload = self.context.recording_manager.status()
        payload["system_load"] = read_system_load()
        payload["disk_space"] = read_disk_space(self.context.recording_manager.rosbag_root)
        take_store = getattr(self.context, "take_store", None)
        payload["current_take"] = take_store.current() if take_store is not None else None
        payload["task_status"] = (
            take_store.task_status() if take_store is not None else {"active": True}
        )
        return web.json_response(payload)

    async def _handle_recording_topics(self, _request: web.Request) -> web.Response:
        loop = asyncio.get_running_loop()
        catalog = await loop.run_in_executor(
            None,
            lambda: self.context.recording_manager.current_topic_catalog(refresh=True),
        )
        return web.json_response(catalog)

    async def _handle_recording_directories(self, request: web.Request) -> web.Response:
        path = str(request.query.get("path", "")).strip() or None
        return web.json_response(
            self.context.recording_manager.browse_recording_directories(path)
        )

    async def _handle_recording_directory_select(
        self, request: web.Request
    ) -> web.Response:
        payload = await read_json_body(request)
        path = str(payload.get("path", "")).strip()
        if not path:
            raise ValueError("Field 'path' is required.")
        return web.json_response(
            self.context.recording_manager.select_recording_root(path)
        )

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
        return await self._start_with_preflight(topics=topics, bag_name=bag_name, automation=False)

    async def _handle_recording_stop(self, _request: web.Request) -> web.Response:
        status = self.context.recording_manager.stop()
        take_store = getattr(self.context, "take_store", None)
        take = take_store.complete_current(status) if take_store is not None else None
        return web.json_response({**status, "take": take})

    async def _start_with_preflight(self, *, topics, bag_name, automation: bool) -> web.Response:
        take_store = getattr(self.context, "take_store", None)
        if take_store is not None and not take_store.task_status().get("active"):
            return web.json_response(
                {
                    "error": "No capture task is active.",
                    "speech": "当前没有数采任务，请先说开始任务叠杯子。",
                },
                status=409,
            )
        preflight_service = getattr(self.context, "capture_preflight", None)
        if preflight_service is not None:
            report = await asyncio.to_thread(
                preflight_service.evaluate, topics, refresh_topics=True
            )
            if not report.get("ok"):
                speech = preflight_service.speech(report)
                return web.json_response(
                    {"error": "Recording preflight failed.", "speech": speech, "preflight": report},
                    status=422,
                )
        else:
            report = None
        try:
            take = (
                take_store.reserve_take(
                    bag_name, trigger="voice" if automation else "web"
                )
                if take_store is not None
                else None
            )
        except RuntimeError as exc:
            task_active = bool(
                take_store is not None and take_store.task_status().get("active")
            )
            return web.json_response(
                {
                    "error": str(exc),
                    "speech": (
                        "当前已有一条正在处理中。"
                        if task_active
                        else "当前没有数采任务，请先说开始任务叠杯子。"
                    ),
                },
                status=409,
            )
        actual_name = take["bag_name"] if take is not None else bag_name
        if automation and not actual_name:
            actual_name = time.strftime("looper_record_%Y%m%d_%H%M%S")
        try:
            status = await asyncio.to_thread(
                self.context.recording_manager.start,
                topics=topics,
                bag_name=actual_name,
            )
            if take_store is not None:
                take = take_store.mark_recording(status.get("output_path"))
        except Exception as exc:
            if take_store is not None:
                take_store.fail_start(exc)
            raise
        return web.json_response({
            **status,
            "automation": "openclaw" if automation else None,
            "trigger": "voice" if automation else "web",
            "preflight": report,
            "take": take,
        })

    async def _handle_automation_recording_start(
        self, _request: web.Request
    ) -> web.Response:
        """Start a default-topic recording owned by the local automation agent."""
        current = self.context.recording_manager.status()
        if current.get("recording"):
            return web.json_response(
                {
                    "error": "Recording is already active.",
                    "recording": True,
                    "output_path": current.get("output_path"),
                },
                status=409,
            )
        return await self._start_with_preflight(
            topics=None,
            bag_name=None,
            automation=True,
        )

    async def _handle_automation_recording_stop(
        self, _request: web.Request
    ) -> web.Response:
        """Stop only recordings created through the automation start route."""
        current = self.context.recording_manager.status()
        if not current.get("recording"):
            return web.json_response({"automation": "openclaw", **current})
        output_path = str(current.get("output_path") or "")
        take_store = getattr(self.context, "take_store", None)
        current_take = take_store.current() if take_store is not None else None
        voice_owned = (
            isinstance(current_take, dict)
            and current_take.get("trigger") == "voice"
            and Path(str(current_take.get("bag_path") or "")).name == Path(output_path).name
        )
        if not voice_owned and not Path(output_path).name.startswith("looper_record_"):
            return web.json_response(
                {
                    "error": "A manual recording is active; voice stop refused.",
                    "recording": True,
                    "output_path": output_path,
                },
                status=409,
            )
        status = self.context.recording_manager.stop()
        take = take_store.complete_current(status) if take_store is not None else None
        return web.json_response({"automation": "openclaw", "trigger": "voice", **status, "take": take})

    async def _handle_rosbag_list(self, request: web.Request) -> web.Response:
        scope = str(request.query.get("scope", "all")).strip().lower()
        if scope not in {"all", "current"}:
            return web.json_response({"error": "scope must be all or current"}, status=400)
        library = self._bag_library()
        loop = asyncio.get_event_loop()
        bags = await loop.run_in_executor(
            None,
            lambda: list_rosbag_locations(
                library.locations(scope), self.context.results_root
            ),
        )
        return web.json_response(
            {
                "type": "rosbag_list",
                "rosbag_root": str(self.context.recording_manager.rosbag_root),
                "scope": scope,
                "library_roots": [
                    str(path) for path in self.context.recording_manager.storage_browse_roots()
                ],
                "results_root": str(self.context.results_root),
                "bags": bags,
            }
        )

    async def _handle_rosbag_delete(self, request: web.Request) -> web.Response:
        bag_name = request.match_info.get("bag_name", "").strip()
        if not bag_name or "/" in bag_name or bag_name in (".", ".."):
            return web.json_response({"error": "Invalid bag name."}, status=400)
        try:
            bag_path = self._bag_library().resolve(bag_name)
        except FileNotFoundError:
            return web.json_response({"error": "Bag not found."}, status=404)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        shutil.rmtree(bag_path)
        return web.json_response({"status": "deleted", "bag_name": bag_path.name})

    async def _handle_integrity_run(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        bag_name = str(payload.get("bag_name", "")).strip()
        if not bag_name or bag_name in (".", ".."):
            return web.json_response({"error": "Invalid bag name."}, status=400)
        try:
            bag_path = self._bag_library().resolve(bag_name)
        except FileNotFoundError:
            return web.json_response({"error": "Bag not found."}, status=404)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
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
        bag_name = bag_path.name
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
