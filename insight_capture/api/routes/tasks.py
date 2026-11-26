"""Capture-task selection and operator status routes."""

import asyncio
from functools import partial
from urllib.parse import quote
from pathlib import Path

from aiohttp import web

from insight_capture.api.context import DashboardContext, capture_transition_lock
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
        self._transition = capture_transition_lock(context)

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

    def _recording_conflict(self, action: str, *, own_transition=False) -> web.Response | None:
        busy = self._transition.locked() and not own_transition
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

    async def _handle_delete(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        count = payload.get("expected_recorded_takes")
        if type(count) is not int or count < 0:
            return web.json_response({"message": "请重新打开删除确认。"}, status=400)
        task_id = request.match_info["task_id"]
        conflict = self._recording_conflict("delete")
        if conflict is not None:
            return conflict

        async def delete():
            async with self._transition:
                conflict = self._recording_conflict("delete", own_transition=True)
                if conflict is not None:
                    return conflict
                playback = getattr(self.context, "prepared_playback_manager", None)
                current = playback.status() if playback else {}
                roots = self.context.recording_manager.storage_browse_roots()
                selected_path = None
                missing_selection = False
                if current.get("bag_name"):
                    try:
                        selected_path = await asyncio.to_thread(
                            self.context.bag_library.resolve, current["bag_name"]
                        )
                    except FileNotFoundError:
                        missing_selection = True
                protected = [selected_path] if selected_path and current.get("state") in {"preparing", "playing"} else []
                removed = await asyncio.to_thread(self._delete_data, partial(
                    self.context.take_store.delete_task, task_id, roots, count,
                    protected_paths=protected))
                paths = removed.pop("paths")
                selected = selected_path and any(
                    selected_path.resolve().is_relative_to(Path(path).resolve()) for path in paths
                )
                if (selected or missing_selection) and current.get("state") == "ready":
                    playback.stop()
                return web.json_response({"status": "deleted", "task": removed})

        return await self._complete_deletion(delete())

    async def _handle_take_delete(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        bag_name = payload.get("bag_name")
        try:
            take_id = int(request.match_info["take_id"])
        except ValueError:
            take_id = 0
        if take_id < 1 or not isinstance(bag_name, str) or not bag_name:
            return web.json_response({"message": "请重新打开删除确认。"}, status=400)
        task_id = request.match_info["task_id"]
        conflict = self._recording_conflict("delete")
        if conflict is not None:
            return conflict

        async def delete():
            async with self._transition:
                conflict = self._recording_conflict("delete", own_transition=True)
                if conflict is not None:
                    return conflict
                playback = getattr(self.context, "prepared_playback_manager", None)
                current = playback.status() if playback else {}
                selected_path = None
                if current.get("bag_name"):
                    try:
                        selected_path = await asyncio.to_thread(self.context.bag_library.resolve, current["bag_name"])
                    except FileNotFoundError:
                        pass
                protected = [selected_path] if selected_path and current.get("state") in {"preparing", "playing"} else []
                removed = await asyncio.to_thread(self._delete_data, partial(
                    self.context.take_store.delete_take, task_id, take_id, bag_name,
                    self.context.recording_manager.storage_browse_roots(), protected_paths=protected))
                if selected_path and current.get("state") == "ready" and any(
                        selected_path.resolve().is_relative_to(Path(path).resolve()) for path in removed["paths"]):
                    playback.stop()
                return web.json_response({"status": "deleted", "take_id": take_id, "task_id": task_id})

        return await self._complete_deletion(delete())

    def _delete_data(self, operation):
        manager = self.context.recording_manager
        delete_saved = getattr(manager, "delete_saved_recordings", None)
        return delete_saved(operation) if delete_saved else operation()

    async def _complete_deletion(self, operation) -> web.Response:
        deletion = asyncio.create_task(operation)
        try:
            return await asyncio.shield(deletion)
        except asyncio.CancelledError:
            await deletion
            raise
        except (FileNotFoundError, ValueError) as exc:
            return web.json_response({"message": str(exc)}, status=404)
        except RuntimeError as exc:
            return web.json_response({"message": str(exc)}, status=409)
        except OSError:
            return web.json_response({"message": "删除未完成，请检查存储后重试。"}, status=500)

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

    async def _handle_end(self, request: web.Request) -> web.Response:
        payload = await read_json_body(request)
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return web.json_response(
                {"message": "页面已更新，请刷新后再结束任务。"}, status=400
            )
        conflict = self._recording_conflict("end")
        if conflict is not None:
            return conflict
        previous = self.context.take_store.task_status()
        if ((previous.get("task") or {}).get("task_id") or previous.get("last_task_id")) != task_id:
            return web.json_response(
                {"message": "当前任务已变化，请重新打开任务列表后操作。"}, status=409
            )
        if not previous.get("active"):
            return web.json_response({**previous, "message": task_status_message(previous)})
        previous_task = previous.get("task") or {}
        name = str(
            previous_task.get("name")
            or "当前"
        )
        recorded = int((previous.get("stats") or {}).get("recorded_takes") or 0)
        status = self.context.take_store.end_task(expected_task_id=task_id)
        return web.json_response(
            {**status, "message": f"已退出{name}任务集，累计录制{recorded}条。"}
        )
