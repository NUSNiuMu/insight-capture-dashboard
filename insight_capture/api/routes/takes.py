"""Operator Take decisions that preserve raw bags."""

from aiohttp import web

from insight_capture.api.context import DashboardContext
from insight_capture.api.support import read_json_body


class TakeRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_reject_current(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        reason = str(payload.get("reason") or "operator_rejected")
        try:
            take = self.context.take_store.reject_current(reason)
        except RuntimeError:
            return web.json_response({
                "type": "take_decision", "ok": False,
                "speech": "当前没有可作废的记录。",
            }, status=409)
        return web.json_response({
            "type": "take_decision", "ok": True,
            "speech": f"第{take['take_id']}条已标记作废，原始数据已保留。",
            "take": take,
        })
