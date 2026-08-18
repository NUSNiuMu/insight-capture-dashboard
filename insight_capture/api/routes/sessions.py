"""Session metadata routes."""

from aiohttp import web
from pathlib import Path

from insight_capture.api.context import DashboardContext


class SessionRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_list(self, _request: web.Request) -> web.Response:
        payload = self.context.take_store.snapshot()
        library = getattr(self.context, "bag_library", None)
        if library is not None:
            for take in payload.get("takes", []):
                bag_path = str(take.get("bag_path") or "").strip()
                take["bag_id"] = (
                    library.reference_for_path(Path(bag_path)) if bag_path else None
                )
        return web.json_response(payload)
