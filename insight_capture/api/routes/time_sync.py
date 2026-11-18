"""Set the offline capture clock from a connected phone while capture is idle."""

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

    async def snapshot(self, _request):
        return web.json_response({**deepcopy(self.context.phone_time_sync),
                                  "server_time": time.time()}, headers={"Cache-Control": "no-store"})

    async def synchronize(self, request):
        received = time.monotonic()
        body = await read_json_body(request)
        epoch_ms, rtt_ms = body.get("epoch_ms"), body.get("rtt_ms", 0)
        if (type(epoch_ms) not in (int, float) or not math.isfinite(epoch_ms)
                or not 946684800000 <= epoch_ms < 4102444800000):
            raise ValueError("手机日期无效，请检查手机系统时间。")
        if (type(rtt_ms) not in (int, float) or not math.isfinite(rtt_ms)
                or not 0 <= rtt_ms <= 2000):
            raise ValueError("网络延迟较大，稍后重新校时。")
        resync = body.get('resync_cameras', False)
        if type(resync) is not bool:
            raise ValueError('resync_cameras must be a boolean')
        if self.transition.locked() or (self._task and not self._task.done()):
            raise RuntimeError("设备忙，空闲后自动校时。")
        # A disconnected browser must not abandon camera recovery halfway.
        self.context.phone_time_sync = {"state": "syncing", "source": "phone"}
        self._task = asyncio.create_task(self._synchronize(epoch_ms / 1000, rtt_ms / 2000, received, resync))
        return web.json_response({"state": "syncing"}, status=202)

    async def _synchronize(self, epoch, half_rtt, received, resync=False):
        try:
            if self.transition.locked():
                self.context.phone_time_sync = {"state": "deferred", "source": "phone"}
                return
            async with self.transition:
                result = await asyncio.to_thread(self._host_sync, {
                    "epoch_s": epoch + half_rtt,
                    "received_monotonic_s": received,
                    "resync_cameras": resync,
                })
                self.context.phone_time_sync = {**result, "source": "phone"}
        except Exception as exc:
            self.context.phone_time_sync = {"state": "error", "source": "phone", "error": str(exc)}
            self.context.node.get_logger().warning(f"Phone time synchronization: {exc}")

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
