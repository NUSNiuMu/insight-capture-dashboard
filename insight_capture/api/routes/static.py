"""StaticRoutes HTTP handlers."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from insight_capture.api.context import DashboardContext


class StaticRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "sessions.html")

    async def _handle_spatial_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "3d.html")

    async def _handle_recording_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "recording.html")

    async def _handle_images_page(self, _request: web.Request) -> web.FileResponse:
        raise web.HTTPFound("/3d")

    async def _handle_bags_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "bags.html")

    async def _handle_umi_dataset_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "umi-dataset.html")

    async def _handle_scoring_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "scoring.html")

    async def _handle_optimization_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "optimization.html")

    async def _handle_handpose_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "handpose.html")

    async def _handle_settings_page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.context.web_root / "settings.html")

    async def _handle_asset(self, request: web.Request) -> web.StreamResponse:
        raw_path = request.query.get("path", "").strip()
        if not raw_path:
            raise web.HTTPBadRequest(text="missing path")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (self.context.project_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        allowed_roots = [self.context.project_root, self.context.project_root.parent]
        for root in allowed_roots:
            try:
                candidate.relative_to(root)
                break
            except ValueError:
                continue
        else:
            raise web.HTTPForbidden(text="path outside allowed roots")
        if not candidate.is_file():
            raise web.HTTPNotFound(text="asset not found")
        response = web.FileResponse(candidate)
        if request.query.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response
