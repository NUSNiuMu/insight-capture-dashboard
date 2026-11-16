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
        identity = {}
        if "task_id" in payload or "take_id" in payload:
            task_id, take_id = payload.get("task_id"), payload.get("take_id")
            if (not isinstance(task_id, str) or not task_id.strip()
                    or type(take_id) is not int or take_id < 1):
                return web.json_response({
                    "type": "take_decision", "ok": False,
                    "message": "录次标识无效，请刷新状态后再作废。",
                }, status=400)
            identity = {"expected_task_id": task_id, "expected_take_id": take_id}
        try:
            take = self.context.take_store.reject_current(reason, **identity)
        except RuntimeError:
            return web.json_response({
                "type": "take_decision", "ok": False,
                "message": ("录次已变化，作废请求未执行，请刷新状态。"
                            if identity else "当前没有可作废的记录。"),
            }, status=409)
        return web.json_response({
            "type": "take_decision", "ok": True,
            "message": f"第{take['take_id']}条已标记作废，原始数据已保留。",
            "take": take,
        })
