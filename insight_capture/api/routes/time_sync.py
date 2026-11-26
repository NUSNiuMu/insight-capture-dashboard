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
from insight_capture.runtime.discovery import phone_clock_status


def elapsed_ms(started):
    return round((time.monotonic() - started) * 1000)


def recovery_hosts(clocks):
    devices = list(clocks.get("devices", {}).values())
    failed = [device for device in devices if not device.get("ok")]
    if not failed:
        # Both clocks can pass individually while their combined bound fails.
        failed = sorted(devices, key=lambda device: device.get("offset_bound_ms", 0), reverse=True)[:2]
    hosts = sorted({device["host"] for device in failed if device.get("host")})
    if not hosts:
        raise RuntimeError("无法识别需校时的 USB 相机。")
    return hosts


class TimeSyncRoutes:
    def __init__(self, context):
        self.context = context
        self.transition = capture_transition_lock(context)
        self._task = None
        context.phone_time_sync = phone_clock_status()
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
        if not resync and self.context.phone_time_sync["state"] == "synced":
            return web.json_response(self.context.phone_time_sync)
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
        started = time.monotonic()
        async with self.transition:
            status = await asyncio.to_thread(self.context.recording_manager.status)
            if (status.get("recording") or status.get("merge_state") in {"finalizing", "merging"}
                    or self.context.prepared_playback_manager.status()["state"] == "playing"):
                raise RuntimeError("设备忙，空闲后再校时。")
            timings = {"idle_check_ms": elapsed_ms(started)}
            if recover:
                result = {"state": "syncing", "source": "cameras", "timings_ms": dict(timings)}
                self.context.camera_time_sync = result
                self._task = asyncio.create_task(
                    self._synchronize(time.time(), started, resync=True, timings=timings)
                )
                return web.json_response(result, status=202)

            started = time.monotonic()
            clocks = await asyncio.to_thread(self._measure_camera_clocks)
            timings["clock_check_ms"] = elapsed_ms(started)
            result = {"state": "synced" if clocks["ok"] else "needs_recovery",
                      "source": "cameras", "timings_ms": timings}
            self.context.camera_time_sync = result
            return web.json_response({**result, "clock": clocks})

    async def _recover_cameras(self, timings):
        started = time.monotonic()
        clocks = await asyncio.to_thread(self._measure_camera_clocks)
        timings["initial_clock_check_ms"] = elapsed_ms(started)
        result = {"state": "synced"}
        if not clocks["ok"]:
            started = time.monotonic()
            try:
                result = await asyncio.to_thread(self._host_sync, {
                    "epoch_s": time.time(),
                    "received_monotonic_s": time.monotonic(),
                    "resync_cameras": True,
                    "camera_hosts": recovery_hosts(clocks),
                })
            finally:
                timings["host_recovery_ms"] = elapsed_ms(started)
            timings.update(result.get("timings_ms", {}))
            if result["state"] != "synced":
                return result

            # USB NTP is requested asynchronously. Keep the admission lock
            # until a fresh measurement passes or the recovery window closes.
            started = time.monotonic()
            deadline = started + 12
            while True:
                clocks = await asyncio.to_thread(self._measure_camera_clocks)
                timings["clock_convergence_ms"] = elapsed_ms(started)
                if clocks["ok"]:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("相机校时未达到录制门槛。")
                await asyncio.sleep(0.5)

        started = time.monotonic()
        pipeline = getattr(self.context.node, "_image_pipeline", None)
        deadline = started + 4
        while pipeline is not None and not pipeline.clock_status()["ok"]:
            if time.monotonic() >= deadline:
                timings["image_validation_ms"] = elapsed_ms(started)
                raise RuntimeError("相机时钟已对齐，图像时间戳尚未恢复。")
            await asyncio.sleep(0.25)
        timings["image_validation_ms"] = elapsed_ms(started)
        return result

    async def _synchronize(self, epoch, received, resync=False, timings=None):
        key, source = ("camera_time_sync", "cameras") if resync else ("phone_time_sync", "phone")
        initial_offset = epoch + time.monotonic() - received - time.time()
        try:
            if self.transition.locked():
                setattr(self.context, key, {"state": "deferred", "source": source})
                return
            async with self.transition:
                if resync:
                    result = await self._recover_cameras(timings)
                    timings["total_ms"] = elapsed_ms(received)
                    result = {**result, "timings_ms": timings}
                    self.context.node.get_logger().info(
                        f"Camera time recovery: {json.dumps(timings)}"
                    )
                else:
                    result = await asyncio.to_thread(self._host_sync, {
                        "epoch_s": epoch, "received_monotonic_s": received, "resync_cameras": False,
                    })
                setattr(self.context, key, {**result, "source": source})
                for name, error in result.get("camera_recovery_errors", {}).items():
                    self.context.node.get_logger().warning(f"Camera recovery after Jetson clock sync ({name}): {error}")
        except Exception as exc:
            now = time.time()
            offset = epoch + time.monotonic() - received - now
            if not resync and abs(initial_offset) > 1 and abs(offset) <= 1:
                # A legacy host helper may step the clock before camera recovery fails.
                self.context.phone_time_sync = {
                    "state": "synced", "source": source, "adjusted": True,
                    "synced_at_epoch_s": now, "correction_s": initial_offset,
                    "camera_recovery_errors": {"host": str(exc)},
                }
                self.context.node.get_logger().warning(f"Jetson clock aligned after incomplete camera recovery: {exc}")
                return
            error = {"state": "error", "source": source, "error": str(exc)}
            if resync:
                timings["total_ms"] = elapsed_ms(received)
                error["timings_ms"] = timings
            setattr(self.context, key, error)
            self.context.node.get_logger().warning(
                f"Time synchronization ({source}): {exc}" + (f"; timings_ms={timings}" if resync else "")
            )

    @staticmethod
    def _host_sync(body):
        # Only this fixed, local clock operation crosses into the root host helper.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            # Legacy cameras have two sequential 165-second control calls.
            connection.settimeout(350 if body.get("resync_cameras") else 180)
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
