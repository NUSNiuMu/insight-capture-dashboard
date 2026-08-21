"""Capture-task selection and operator status routes."""

from aiohttp import web

from insight_capture.api.context import DashboardContext


def task_status_speech(status: dict) -> str:
    if not status.get("active"):
        return "当前没有进行中的数采任务。请先说开始任务叠杯子。"
    task = status.get("task") or {}
    stats = status.get("stats") or {}
    name = str(task.get("speech_name") or task.get("name") or "未命名任务")
    recorded = int(stats.get("recorded_takes") or 0)
    valid = int(stats.get("valid_takes") or 0)
    rejected = int(stats.get("rejected_takes") or 0)
    next_take = int(stats.get("next_take_id") or recorded + 1)
    return (
        f"当前任务集是{name}，累计已录制{recorded}条，"
        f"有效{valid}条，作废{rejected}条，下一条是第{next_take}条。"
    )


class TaskRoutes:
    def __init__(self, context: DashboardContext) -> None:
        self.context = context

    async def _handle_list(self, _request: web.Request) -> web.Response:
        status = self.context.take_store.task_status()
        task = status.get("task") or {}
        return web.json_response({
            "tasks": self.context.take_store.list_tasks(),
            "active_task_id": task.get("task_id") if status.get("active") else None,
        })

    async def _handle_current(self, _request: web.Request) -> web.Response:
        status = self.context.take_store.task_status()
        return web.json_response({**status, "speech": task_status_speech(status)})

    async def _handle_activate(self, request: web.Request) -> web.Response:
        if self.context.recording_manager.status().get("recording"):
            return web.json_response(
                {"error": "Cannot change task while recording.", "speech": "正在录制，不能切换任务。"},
                status=409,
            )
        task_id = request.match_info.get("task_id", "").strip()
        previous = self.context.take_store.task_status()
        status = self.context.take_store.activate_task(task_id)
        unchanged = (
            previous.get("active")
            and (previous.get("task") or {}).get("task_id") == task_id
            and previous.get("session_id") == status.get("session_id")
        )
        active_task = status.get("task") or {}
        speech_name = active_task.get("speech_name") or active_task.get(
            "name", "数采"
        )
        speech = (
            task_status_speech(status)
            if unchanged
            else (
                f"已进入{speech_name}任务，"
                f"下一条是第{(status.get('stats') or {}).get('next_take_id', 1)}条。"
            )
        )
        return web.json_response({**status, "changed": not unchanged, "speech": speech})

    async def _handle_end(self, _request: web.Request) -> web.Response:
        if self.context.recording_manager.status().get("recording"):
            return web.json_response(
                {"error": "Cannot end task while recording.", "speech": "正在录制，不能结束任务。"},
                status=409,
            )
        previous = self.context.take_store.task_status()
        if not previous.get("active"):
            return web.json_response({**previous, "speech": task_status_speech(previous)})
        previous_task = previous.get("task") or {}
        name = str(
            previous_task.get("speech_name")
            or previous_task.get("name")
            or "当前"
        )
        recorded = int((previous.get("stats") or {}).get("recorded_takes") or 0)
        status = self.context.take_store.end_task()
        return web.json_response(
            {**status, "speech": f"已退出{name}任务集，累计录制{recorded}条。"}
        )
