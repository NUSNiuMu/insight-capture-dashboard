"""Keep phone clock setup separate from camera clock checks and recovery."""

import asyncio
import math
import json
import socket
import time
from copy import deepcopy

from aiohttp import web

from insight_capture.api.context import capture_transition_lock
from insight_capture.api.support import read_json_body


class TimeSyncRoutes:
    def __init__(self, context):
        self.context = context
        self.transition = capture_transition_lock(context)
        self._task = None
        context.phone_time_sync = {"state": "idle", "source": "phone"}
        context.camera_time_sync = {"state": "idle", "source": "cameras"}

    async def snapshot(self, _request):
        return web.json_response({**deepcopy(self.context.phone_time_sync),
                                  "server_time": time.time()}, headers={"Cache-Control": "no-store"})

    async def synchronize(self, request):
        received = time.monotonic()
        body = await read_json_body(request)
        resync = body.get("resync_cameras", False)
        if type(resync) is not bool:
            raise ValueError("resync_cameras must be a boolean")
        recover = body.get("recover_cameras", False)
        if type(recover) is not bool or (recover and not resync):
            raise ValueError("recover_cameras requires a camera time check")
        if self.transition.locked() or (self._task and not self._task.done()):
            raise RuntimeError("设备忙，空闲后再校时。")
        if resync:
            return await self._check_cameras(recover)
        epoch_ms, rtt_ms = body.get("epoch_ms"), body.get("rtt_ms", 0)
        if (type(epoch_ms) not in (int, float) or not math.isfinite(epoch_ms)
                or not 946684800000 <= epoch_ms < 4102444800000):
            raise ValueError("手机日期无效，请检查手机系统时间。")
        if (type(rtt_ms) not in (int, float) or not math.isfinite(rtt_ms)
                or not 0 <= rtt_ms <= 2000):
            raise ValueError("网络延迟较大，稍后重新校时。")
        epoch = epoch_ms / 1000 + rtt_ms / 2000
        if self.context.phone_time_sync["state"] == "error":
            now = time.time()
            offset = epoch + time.monotonic() - received - now
            if abs(offset) > 1:
                return web.json_response(self.context.phone_time_sync, status=409)
            result = {"state": "synced", "source": "phone", "synced_at_epoch_s": now,
                      "correction_s": offset}
            self.context.phone_time_sync = result
            return web.json_response(result)
        self.context.phone_time_sync = {"state": "syncing", "source": "phone"}
        self._task = asyncio.create_task(self._synchronize(epoch, received))
        return web.json_response({"state": "syncing"}, status=202)

    def _measure_camera_clocks(self):
        manager = self.context.recording_manager
        measure = getattr(manager, "measure_camera_clocks", None)
        if measure:
            return measure()
        if manager.clock_monitor:
            return manager.clock_monitor.measure()
        raise RuntimeError("相机校时监测未启用。")

    async def _check_cameras(self, recover):
        async with self.transition:
            status = await asyncio.to_thread(self.context.recording_manager.status)
            if (status.get("recording") or status.get("merge_state") in {"finalizing", "merging"}
                    or self.context.prepared_playback_manager.status()["state"] == "playing"):
                raise RuntimeError("设备忙，空闲后再校时。")
            clocks = await asyncio.to_thread(self._measure_camera_clocks)
            if clocks["ok"]:
                result = {"state": "synced", "source": "cameras"}
                self.context.camera_time_sync = result
                return web.json_response({**result, "clock": clocks})
            if not recover:
                result = {"state": "needs_recovery", "source": "cameras"}
                self.context.camera_time_sync = result
                return web.json_response({**result, "clock": clocks})
            result = {"state": "syncing", "source": "cameras"}
            self.context.camera_time_sync = result
            # The host uses its own clock for camera recovery.
            self._task = asyncio.create_task(self._synchronize(time.time(), time.monotonic(), resync=True))
            return web.json_response(result, status=202)

    async def _synchronize(self, epoch, received, resync=False):
        key, source = ("camera_time_sync", "cameras") if resync else ("phone_time_sync", "phone")
        try:
            if self.transition.locked():
                setattr(self.context, key, {"state": "deferred", "source": source})
                return
            async with self.transition:
                result = await asyncio.to_thread(self._host_sync, {
                    "epoch_s": epoch,
                    "received_monotonic_s": received,
                    "resync_cameras": resync,
                })
                if resync and result["state"] == "synced":
                    clocks = await asyncio.to_thread(self._measure_camera_clocks)
                    if not clocks["ok"]:
                        raise RuntimeError("相机校时未达到录制门槛。")
                setattr(self.context, key, {**result, "source": source})
        except Exception as exc:
            setattr(self.context, key, {"state": "error", "source": source, "error": str(exc)})
            self.context.node.get_logger().warning(f"Time synchronization ({source}): {exc}")

    @staticmethod
    def _host_sync(body):
        # Only this fixed, local clock operation crosses into the root host helper.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(180)
            connection.connect("/run/insight-capture-network/phone-clock.sock")
            connection.sendall(json.dumps(body).encode() + b"\n")
            with connection.makefile("rb") as stream:
                result = json.loads(stream.readline(8192))
            if result.get("state") == "error":
                raise RuntimeError(result.get("error", "手机校时失败。"))
            return result

    async def close(self, _app):
        if self._task:
            await self._task
