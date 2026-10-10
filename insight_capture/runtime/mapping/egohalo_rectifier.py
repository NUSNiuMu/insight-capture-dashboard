"""Decode the EgoHalo stereo pair and publish calibrated pinhole inputs."""

import argparse
import copy
import json
import os
import queue
import threading
from pathlib import Path

import numpy as np

from insight_capture.media.h264_decode import H264Decoder
from insight_capture.media.h264 import H264ReferenceSequence
from insight_capture.runtime.mapping.fisheye import FisheyeRectification
from insight_capture.runtime.mapping.geometry import matrix_from_transform
from insight_capture.runtime.ros.network import configure_ros_network


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    settings = config["egohalo_rectification"]
    os.environ["ROS_DOMAIN_ID"] = str(config["ros_domain_id"])
    configure_ros_network(config)
    import rclpy
    from rclpy.time import Time
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CameraInfo, CompressedImage, Image
    from geometry_msgs.msg import TransformStamped
    from tf2_ros import Buffer, TransformListener, StaticTransformBroadcaster
    from scipy.spatial.transform import Rotation

    rclpy.init()
    node = rclpy.create_node("egohalo_stereo_rectifier")
    namespace = settings["namespace"]
    base = f"/{namespace}/camera/stereo"
    frames = [f"{namespace}_camera_stereo_{side}" for side in ("left", "right")]
    rect_frames = [config["mapping"][key + "_frame"] for key in ("left", "right")]
    buffer = Buffer()
    _listener = TransformListener(buffer, node)
    broadcaster = StaticTransformBroadcaster(node)
    infos = [None, None]
    calibration = [None]
    queues = [queue.Queue(maxsize=8), queue.Queue(maxsize=8)]
    reset = [threading.Event(), threading.Event()]
    stop = threading.Event()
    failed = threading.Event()
    source_fps = float(settings.get("source_fps", 30.0))
    output_hz = float(settings["output_hz"])
    if not 0 < output_hz <= source_fps:
        raise ValueError("Rectification requires 0 < output_hz <= source_fps")
    max_gap_ns = int(1.5e9 / source_fps)
    sample_tolerance_ns = int(.54e9 / source_fps)
    publishers, info_publishers = [], []
    for side in ("left", "right"):
        publishers.append(node.create_publisher(Image, config["mapping"][side + "_image_topic"], 2))
        info_publishers.append(node.create_publisher(CameraInfo, config["mapping"][side + "_info_topic"], 2))

    def configure():
        if calibration[0] is not None or any(info is None for info in infos):
            return
        try:
            transform = buffer.lookup_transform(frames[1], frames[0], Time()).transform
            matrix = matrix_from_transform(
                (transform.translation.x, transform.translation.y, transform.translation.z),
                (transform.rotation.x, transform.rotation.y, transform.rotation.z, transform.rotation.w),
            )
            value = FisheyeRectification.create(
                *infos, matrix, size=(settings["output_width"], settings["output_height"]),
                horizontal_fov_deg=float(settings.get("horizontal_fov_deg", 90.0)),
            )
            transforms = []
            for index in range(2):
                transform = TransformStamped()
                transform.header.stamp = node.get_clock().now().to_msg()
                transform.header.frame_id = frames[index]
                transform.child_frame_id = rect_frames[index]
                quat = Rotation.from_matrix(value.rotations[index].T).as_quat()
                transform.transform.rotation.x = float(quat[0])
                transform.transform.rotation.y = float(quat[1])
                transform.transform.rotation.z = float(quat[2])
                transform.transform.rotation.w = float(quat[3])
                transforms.append(transform)
            broadcaster.sendTransform(transforms)
            calibration[0] = value
            node.get_logger().info(f"Fisheye stereo rectification ready: {value.size}")
        except Exception as exc:
            node.get_logger().warning(f"Waiting for valid stereo calibration/TF: {exc}", throttle_duration_sec=5)

    def worker(index):
        decoder = None
        headers = {}
        last_output = None
        last_input = None
        sequence = H264ReferenceSequence()
        period = int(1e9 / output_hz)
        def selected(pts):
            bucket = int(round(pts / period))
            return calibration[0] is not None and bucket != last_output and abs(pts - bucket * period) <= sample_tolerance_ns
        try:
            decoder = H264Decoder(pixel_format="GRAY8", low_latency=True)
            while not stop.is_set():
                try:
                    message = queues[index].get(timeout=.1)
                except queue.Empty:
                    continue
                stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
                data = bytes(message.data)
                contiguous = sequence.observe(data)
                capture_gap = contiguous is True and last_input is not None and 0 < stamp - last_input < min(8, sequence.modulus - 1) * 1e9 / source_fps
                if reset[index].is_set() or contiguous is False or (last_input is not None and not 0 < stamp-last_input <= max_gap_ns and not capture_gap):
                    decoder.gate.reset()
                    if last_input is not None and stamp <= last_input:
                        decoder.close()
                        decoder = H264Decoder(pixel_format="GRAY8", low_latency=True)
                        headers.clear()
                        last_output = None
                    reset[index].clear()
                last_input = stamp
                if decoder.push(data, stamp):
                    headers[stamp] = copy.deepcopy(message.header)
                for _ in range(16):
                    result = decoder.pull(5_000_000, select_frame=selected)
                    if result is None:
                        break
                    pts, image = result
                    header = headers.pop(pts, None)
                    value = calibration[0]
                    if header is None or value is None or image is None:
                        continue
                    bucket = int(round(pts / period))
                    # Both eyes select the same exposure using the shared source clock.
                    if bucket == last_output or abs(pts - bucket * period) > sample_tolerance_ns:
                        continue
                    last_output = bucket
                    rectified = value.rectify(index, image)
                    output = Image()
                    header.frame_id = rect_frames[index]
                    output.header = header
                    output.height, output.width = rectified.shape
                    output.encoding = "mono8"
                    output.step = output.width
                    output.data = rectified.tobytes()
                    info = CameraInfo()
                    info.header = header
                    info.width, info.height = value.size
                    info.distortion_model = "plumb_bob"
                    info.d = [0.0] * 5
                    info.k = value.projections[index][:, :3].flatten().tolist()
                    info.r = np.eye(3).flatten().tolist()
                    info.p = value.projections[index].flatten().tolist()
                    info_publishers[index].publish(info)
                    publishers[index].publish(output)
                if len(headers) > 60:
                    raise RuntimeError("Decoder source timestamps are not being matched")
        except Exception as exc:
            node.get_logger().error(f"Stereo decoder {index} failed: {exc}")
            failed.set()
            stop.set()
        finally:
            if decoder is not None:
                decoder.close()

    threads = []
    for index, side in enumerate(("left", "right")):
        def on_info(message, index=index):
            infos[index] = message
        def on_image(message, index=index):
            if message.format.lower() != "h264":
                return
            try:
                queues[index].put_nowait(message)
            except queue.Full:
                reset[index].set()
                while True:
                    try:
                        queues[index].get_nowait()
                    except queue.Empty:
                        break
        node.create_subscription(CameraInfo, base + f"/{side}/camera_info", on_info, qos_profile_sensor_data)
        node.create_subscription(CompressedImage, base + f"/{side}/image_raw/h264", on_image, 8)
        thread = threading.Thread(target=worker, args=(index,), daemon=True)
        thread.start()
        threads.append(thread)
    node.create_timer(.5, configure)
    try:
        while rclpy.ok() and not stop.is_set():
            rclpy.spin_once(node, timeout_sec=.1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=3)
        node.destroy_node()
        rclpy.try_shutdown()
    if failed.is_set():
        raise RuntimeError("Stereo rectifier stopped after a decoder failure")


if __name__ == "__main__":
    main()
