"""OptimizationRoutes HTTP handlers."""

from __future__ import annotations

import asyncio

from aiohttp import web

from insight_capture.postprocess.optimization.inputs import recording_inputs
from insight_capture.api.context import DashboardContext
from insight_capture.api.support import _read_tum_points, read_json_body


class OptimizationRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_optimization_inputs(self, request: web.Request) -> web.Response:
        bag_name = request.query.get("bag_name", "").strip()
        try:
            bag = await asyncio.to_thread(self.context.bag_library.resolve, bag_name)
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text="Bag not found.") from exc
        cameras = await asyncio.to_thread(recording_inputs, bag)
        return web.json_response({"bag_name": bag_name, "cameras": cameras})

    async def _handle_optimization_start(self, request: web.Request) -> web.Response:
        body = await read_json_body(request)
        bag_name = str(body.get("bag_name", "")).strip()
        try:
            bag = await asyncio.to_thread(self.context.bag_library.resolve, bag_name)
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text="Bag not found.") from exc
        cameras = await asyncio.to_thread(recording_inputs, bag)
        camera = next((c for c in cameras if c["name"] == body.get("camera_name")), None)
        if camera is None:
            raise ValueError("Select a camera recorded in this MCAP.")
        stream = next((s for s in camera["streams"] if s["topic"] == body.get("image_topic")), None)
        if stream is None:
            raise ValueError("Select an image stream recorded in this MCAP.")
        view = stream["topic"].split("/camera/", 1)[1].split("/image_", 1)[0].replace("/", "_")
        run_name = str(body.get("run_name", "")).strip() or f"{bag.name}__{camera['name']}__{view}"
        await asyncio.to_thread(self.context.optimization_manager.start, bag, run_name,
                                camera["vio_topic"], stream["topic"], stream["camera_info_topic"])
        return web.json_response({"status": "running", "run_name": run_name,
                                  "camera": camera["name"], "image_topic": stream["topic"]})

    async def _handle_optimization_stop(self, _request: web.Request) -> web.Response:
        await asyncio.to_thread(self.context.optimization_manager.stop)
        return web.json_response({"status": "idle"})

    async def _handle_optimization_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.optimization_manager.status())

    async def _handle_optimization_trajectories(self, request: web.Request) -> web.Response:
        run_name = request.rel_url.query.get("run_name", "").strip()
        if not run_name or run_name in {".", ".."} or "/" in run_name:
            raise ValueError("Run name must be a directory name.")
        project_root = self.context.project_root
        vio_path = project_root / "data" / "derived" / run_name / "trajectories" / "vio_100hz.tum"
        colmap_path = next((project_root / "runs" / run_name).glob("viz/*/colmap_sim3.tum"), None)
        return web.json_response({
            "vio": _read_tum_points(vio_path),
            "colmap": _read_tum_points(colmap_path) if colmap_path is not None else [],
        })

    async def _handle_optimization_runs(self, _request: web.Request) -> web.Response:
        runs_root = self.context.project_root / "runs"
        entries = []
        if runs_root.exists():
            for run_dir in sorted(runs_root.iterdir()):
                if not run_dir.is_dir():
                    continue
                sim3 = next(run_dir.glob("viz/*/colmap_sim3.tum"), None)
                colmap_tum = next(run_dir.glob("colmap/*/colmap.tum"), None)
                if sim3 or colmap_tum:
                    entries.append({
                        "run_name": run_dir.name,
                        "has_sim3": sim3 is not None,
                    })
        return web.json_response({"runs": entries})
