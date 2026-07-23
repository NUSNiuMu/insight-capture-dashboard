"""OptimizationRoutes HTTP handlers."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from camera_setup import IMAGE_STREAMS
from dashboard_web.support import _read_tum_points

from dashboard_web.context import DashboardContext


class OptimizationRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_optimization_start(self, request: web.Request) -> web.Response:
        body = await request.json()
        bag_name = str(body.get("bag_name", "")).strip()
        camera_name = str(body.get("camera_name", "")).strip()
        stream_type = str(body.get("stream_type", "color_compressed")).strip()
        run_name = str(body.get("run_name", "")).strip() or bag_name
        if not bag_name:
            return web.json_response({"error": "bag_name is required"}, status=400)
        if stream_type not in IMAGE_STREAMS:
            return web.json_response(
                {"error": f"Unknown stream_type '{stream_type}'. Valid: {list(IMAGE_STREAMS.keys())}"},
                status=400,
            )
        if camera_name:
            cam = next((c for c in self.context.node.cameras if c.name == camera_name), None)
            if cam is None:
                available = [c.name for c in self.context.node.cameras]
                return web.json_response(
                    {"error": f"Camera '{camera_name}' not found. Available: {available}"},
                    status=400,
                )
        else:
            cam = self.context.node.cameras[0] if self.context.node.cameras else None
        if cam is None:
            return web.json_response({"error": "No cameras configured"}, status=400)
        from camera_setup import camera_base, image_topic as mk_image_topic
        vio = f"{camera_base(cam.namespace)}/vio_100hz"
        img = mk_image_topic(cam.namespace, stream_type)
        self.context.optimization_manager.start(bag_name, run_name, vio, img)
        return web.json_response({"status": "running", "run_name": run_name, "camera": cam.name, "stream": stream_type, "image_topic": img})

    async def _handle_optimization_stop(self, _request: web.Request) -> web.Response:
        self.context.optimization_manager.stop()
        return web.json_response({"status": "idle"})

    async def _handle_optimization_status(self, _request: web.Request) -> web.Response:
        return web.json_response(self.context.optimization_manager.status())

    async def _handle_optimization_trajectories(self, request: web.Request) -> web.Response:
        run_name = request.rel_url.query.get("run_name", "").strip()
        if not run_name:
            return web.json_response({"error": "run_name required"}, status=400)
        project_root = self.context.project_root
        hz_label = "5"
        vio_path = project_root / "data" / "derived" / run_name / "trajectories" / "vio_100hz.tum"
        colmap_path = project_root / "runs" / run_name / "viz" / f"color_{hz_label}hz_vs_vio100" / "colmap_sim3.tum"
        return web.json_response({
            "vio": _read_tum_points(vio_path),
            "colmap": _read_tum_points(colmap_path),
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
