"""Capture-task selection and operator status routes."""

import asyncio
from urllib.parse import quote

from aiohttp import web

from insight_capture.api.context import DashboardContext
from insight_capture.api.support import read_json_body
from insight_capture.runtime.recovered_bundle import recovered_bundle


def task_status_message(status: dict) -> str:
    if not status.get("active"):
        return "当前没有进行中的数采任务。请先选择并启用数采任务。"
    task = status.get("task") or {}
    stats = status.get("stats") or {}
    name = str(task.get("name") or "未命名任务")
    recorded = int(stats.get("recorded_takes") or 0)
    valid = int(stats.get("valid_takes") or 0)
    rejected = int(stats.get("rejected_takes") or 0)
    next_take = int(stats.get("next_take_id") or recorded + 1)
    return (
        f"当前任务集是{name}，累计已录制{recorded}条，"
        f"有效{valid}条，作废{rejected}条，下一条是第{next_take}条。"
    )


class TaskRoutes:
    def __init__(self, context: DashboardContext, recording=None) -> None:
        self.context = context
        self.recording = recording

    async def _handle_list(self, _request: web.Request) -> web.Response:
        def snapshot():
            status = self.context.take_store.task_status()
            task = status.get("task") or {}
            return {
                "tasks": self.context.take_store.list_tasks(),
                "active_task_id": task.get("task_id") if status.get("active") else None,
            }
        return web.json_response(await asyncio.to_thread(snapshot))

    async def _handle_recordings(self, request: web.Request) -> web.Response:
        payload = await asyncio.to_thread(
            self.context.take_store.task_recordings, request.match_info["task_id"]
        )
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def _handle_recovered_file(self, request: web.Request) -> web.StreamResponse:
        kind = request.match_info['kind']
        try:
            def resolve():
                return [self.context.take_store.recovered_file(
                    request.match_info['task_id'], int(request.match_info['take_id']), item
                ) for item in (('data', 'metadata', 'manifest') if kind == 'bundle' else (kind,))]
            paths = await asyncio.to_thread(resolve)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise web.HTTPNotFound(text='Recovered file unavailable') from exc
        if kind == 'bundle':
            response = web.StreamResponse(headers={
                'Content-Type': 'application/zip',
                'Content-Disposition': "attachment; filename*=UTF-8''" + quote(paths[0].parent.name + '.zip', safe=''),
                'Cache-Control': 'no-store',
            })
            await response.prepare(request)
            if request.method != 'HEAD':
                chunks = recovered_bundle(paths)
                try:
                    while True:
                        pending = asyncio.create_task(asyncio.to_thread(next, chunks, None))
                        try:
                            data = await asyncio.shield(pending)
                        except asyncio.CancelledError:
                            # Finish the bounded read before closing its generator.
                            await pending
                            raise
                        if data is None:
                            break
                        await response.write(data)
                except ConnectionError:
                    # Cancelling a browser download is a normal client action.
                    return response
                finally:
                    chunks.close()
            await response.write_eof()
            return response
        path = paths[0]
        return web.FileResponse(path, headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(path.name, safe=''),
            'Cache-Control': 'no-store',
        })

    async def _handle_current(self, _request: web.Request) -> web.Response:
        status = await asyncio.to_thread(self.context.take_store.task_status)
        return web.json_response({**status, "message": task_status_message(status)})

    def _recording_conflict(self, action: str) -> web.Response | None:
        busy = bool(self.recording and self.recording.busy)
        recording = {} if busy else self.context.recording_manager.status()
        if (not busy
                and not recording.get("recording")
                and recording.get("merge_state") not in {"merging", "finalizing"}):
            return None
        return web.json_response(
            {
                "error": f"Cannot {action} a task while recording.",
                "message": "正在录制，不能修改任务。",
            },
            status=409,
        )

    async def _handle_create(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        conflict = self._recording_conflict("create")
        if conflict is not None:
            return conflict
        task = self.context.take_store.create_task(payload)
        return web.json_response({"task": task}, status=201)

    async def _handle_update(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        conflict = self._recording_conflict("edit")
        if conflict is not None:
            return conflict
        task_id = request.match_info.get("task_id", "").strip()
        task = self.context.take_store.update_task(
            task_id, payload
        )
        return web.json_response({"task": task})

    async def _handle_activate(self, request: web.Request) -> web.Response:
        conflict = self._recording_conflict("change")
        if conflict is not None:
            return conflict
        task_id = request.match_info.get("task_id", "").strip()
        previous = self.context.take_store.task_status()
        status = self.context.take_store.activate_task(task_id)
        unchanged = (
            previous.get("active")
            and (previous.get("task") or {}).get("task_id") == task_id
            and previous.get("session_id") == status.get("session_id")
        )
        active_task = status.get("task") or {}
        name = active_task.get("name", "数采")
        message = (
            task_status_message(status)
            if unchanged
            else (
                f"已进入{name}任务，"
                f"下一条是第{(status.get('stats') or {}).get('next_take_id', 1)}条。"
            )
        )
        return web.json_response({**status, "changed": not unchanged, "message": message})

    async def _handle_end(self, _request: web.Request) -> web.Response:
        conflict = self._recording_conflict("end")
        if conflict is not None:
            return conflict
        previous = self.context.take_store.task_status()
        if not previous.get("active"):
            return web.json_response({**previous, "message": task_status_message(previous)})
        previous_task = previous.get("task") or {}
        name = str(
            previous_task.get("name")
            or "当前"
        )
        recorded = int((previous.get("stats") or {}).get("recorded_takes") or 0)
        status = self.context.take_store.end_task()
        return web.json_response(
            {**status, "message": f"已退出{name}任务集，累计录制{recorded}条。"}
        )
