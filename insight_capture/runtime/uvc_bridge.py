"""Publish wrist cameras and keep each automatic UQ2168 source stable during capture."""

import json
import os
from pathlib import Path
import threading
import time

from insight_capture.runtime.camera_profiles import (
    BINDINGS, ROLES, UQ2168_LABELS, bound_uvc, capture_lock, capture_status,
    idle, read_json, uq2168_devices, uvc_devices,
)
from insight_capture.runtime import uq2168
from insight_capture.core.paths import PROJECT_ROOT


class AutomaticUvcSources:
    def __init__(self, config, port):
        self.config = config
        self.port = port
        self.assignments = {}
        self.lock = threading.Lock()

    def select(self, role):
        with self.lock:
            devices = {camera["attachment"]: camera for camera in uq2168_devices(uvc_devices())}
            current = devices.get(self.assignments.get(role))
            if current is not None:
                return current
            vacant = [slot for slot in ROLES if self.assignments.get(slot) not in devices]
            available = [key for key in devices if key not in self.assignments.values()]
            index = vacant.index(role)
            if index >= len(available):
                return None
            try:
                # Start holds the shared lock through preflight and recorder
                # admission. Check actual capture state while excluding a new start.
                with capture_lock(self.config):
                    if idle(capture_status(self.port)):
                        key = available[index]
                        # Only the reader of this slot can replace it, after it
                        # has stopped publishing from its previous device.
                        self.assignments[role] = key
                        return devices[key]
            except (OSError, ValueError):
                pass
            return None


def stream_camera(source, role, publisher, stop, gst, compressed_image):
    sink_spec = "appsink name=frames sync=false max-buffers=2 drop=true"
    # Keep camera-produced JPEG bytes when possible. Raw-only UVC cameras use jpegenc.
    for conversion in ("image/jpeg", "videoconvert ! jpegenc quality=85"):
        pipeline = gst.parse_launch(f"v4l2src device={source} ! {conversion} ! {sink_spec}")
        delivered = False
        try:
            sink = pipeline.get_by_name("frames")
            if pipeline.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
                continue
            while not stop.is_set():
                sample = sink.emit("try-pull-sample", 2 * gst.SECOND)
                if sample is None:
                    break
                buffer = sample.get_buffer()
                stamp = time.time_ns()
                clock = pipeline.get_clock()
                if clock is not None and buffer.pts != gst.CLOCK_TIME_NONE:
                    elapsed = clock.get_time() - pipeline.get_base_time() - buffer.pts
                    if 0 <= elapsed <= 2 * gst.SECOND:
                        stamp -= elapsed
                message = compressed_image()
                message.header.frame_id = f"uvc_{role}_optical"
                message.header.stamp.sec, message.header.stamp.nanosec = divmod(stamp, 10**9)
                message.format = "jpeg"
                message.data = buffer.extract_dup(0, buffer.get_size())
                publisher.publish(message)
                delivered = True
        finally:
            pipeline.set_state(gst.State.NULL)
        if delivered or stop.is_set():
            return


def run_camera(node, role, publisher, stop, gst, compressed_image, gst_error, *,
               raw_publishers=None, bindings=None, automatic_sources=None):
    path = PROJECT_ROOT / "config" / BINDINGS
    while not stop.is_set():
        try:
            device = (automatic_sources.select(role) if automatic_sources is not None
                      else bound_uvc(uvc_devices(), bindings or read_json(path, {})).get(role))
            if device is not None:
                source = Path("/host-dev") / device["node"]
                if raw_publishers is None:
                    stream_camera(source, role, publisher, stop, gst, compressed_image)
                else:
                    attachment = device["attachment"] if automatic_sources is not None else None
                    uq2168.stream(source, role, raw_publishers, stop, attachment=attachment)
        except (OSError, ValueError, KeyError, RuntimeError, gst_error) as exc:
            if not stop.is_set():
                node.get_logger().warning(f"UVC {role}: {exc}")
        stop.wait(1)


def main():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from sensor_msgs.msg import CompressedImage
    from std_msgs.msg import UInt8MultiArray, UInt64MultiArray

    config = json.loads((PROJECT_ROOT / "config" / "cameras.json").read_text())
    native = {camera["name"].removeprefix("uvc_"): camera
              for camera in config["cameras"] if camera.get("uq2168")}
    bindings = {role: camera.get("uvc_binding") for role, camera in native.items()}
    has_pair = set(native) == set(ROLES)
    automatic = has_pair and all(
        not bindings[role] or camera.get("dashboard_label") == UQ2168_LABELS[role]
        for role, camera in native.items()
    )
    automatic_sources = (AutomaticUvcSources(PROJECT_ROOT / "config", int(os.environ.get("DASHBOARD_PORT", 8765)))
                         if automatic else None)
    bindings = bindings if has_pair and all(bindings.values()) else None
    Gst.init(None)
    rclpy.init()
    node = rclpy.create_node("uvc_wrist_inputs")
    stop = threading.Event()
    workers = []
    try:
        for role in ROLES:
            prefix = f"/uvc_{role}/camera"
            publisher = node.create_publisher(CompressedImage, prefix + "/image_raw/compressed", 10)
            raw_publishers = None
            if role in native:
                raw_publishers = {
                    "image": publisher,
                    "frame_timing": node.create_publisher(UInt64MultiArray, prefix + "/frame_timing", 10),
                    "imu_raw": node.create_publisher(UInt8MultiArray, prefix + "/imu_raw", 10),
                    "uvc_metadata": node.create_publisher(UInt8MultiArray, prefix + "/uvc_metadata", 10),
                }
            worker = threading.Thread(target=run_camera, args=(
                node, role, publisher, stop, Gst, CompressedImage, GLib.Error),
                kwargs={"raw_publishers": raw_publishers, "bindings": bindings,
                        "automatic_sources": automatic_sources}, name=f"uvc-{role}")
            worker.start()
            workers.append(worker)
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        stop.set()
        for worker in workers:
            worker.join()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
