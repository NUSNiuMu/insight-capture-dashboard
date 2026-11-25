"""Publish the two bound wrist cameras, reopening V4L2 after hotplug."""

from pathlib import Path
import threading
import time

from insight_capture.runtime.camera_profiles import BINDINGS, ROLES, bound_uvc, read_json, uvc_devices
from insight_capture.core.paths import PROJECT_ROOT


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


def run_camera(node, role, publisher, stop, gst, compressed_image, gst_error):
    path = PROJECT_ROOT / "config" / BINDINGS
    while not stop.is_set():
        try:
            device = bound_uvc(uvc_devices(), read_json(path, {})).get(role)
            if device is not None:
                source = Path("/host-dev") / device["node"]
                stream_camera(source, role, publisher, stop, gst, compressed_image)
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

    Gst.init(None)
    rclpy.init()
    node = rclpy.create_node("uvc_wrist_inputs")
    stop = threading.Event()
    workers = []
    try:
        for role in ROLES:
            publisher = node.create_publisher(CompressedImage, f"/uvc_{role}/camera/image_raw/compressed", 10)
            worker = threading.Thread(target=run_camera, args=(
                node, role, publisher, stop, Gst, CompressedImage, GLib.Error), name=f"uvc-{role}")
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
