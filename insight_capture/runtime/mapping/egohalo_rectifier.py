"""Decode each Ego camera once and publish calibrated overlapping stereo views."""

import argparse
from array import array
import copy
import json
import os
import queue
import threading
import time
from pathlib import Path

import numpy as np
import cv2

from insight_capture.media.h264_decode import H264Decoder
from insight_capture.media.h264 import H264ReferenceSequence
from insight_capture.runtime.mapping.fisheye import FisheyeRectification
from insight_capture.runtime.mapping.geometry import matrix_from_transform
from insight_capture.runtime.mapping.rig import calibration_identity, stereo_views
from insight_capture.runtime.ros.network import configure_ros_network


def run_rectifier(config, stop, sink=None):
    """Decode source AUs continuously; publish only selected calibrated frames."""
    settings = config["egohalo_rectification"]
    # Four source workers already run concurrently; nested OpenCV parallelism
    # competes with capture, pose handling and inference on the Jetson cores.
    cv2.setNumThreads(max(1, int(settings.get("opencv_threads", 1))))
    os.environ["ROS_DOMAIN_ID"] = str(
        config["ros_domain_id"] if sink is None else sink.source_domain
    )
    if sink is None:
        configure_ros_network(config)
    import rclpy
    from rclpy.time import Time
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CameraInfo, CompressedImage, Image
    from geometry_msgs.msg import TransformStamped
    from std_msgs.msg import String
    from tf2_ros import Buffer, TransformListener, StaticTransformBroadcaster
    from scipy.spatial.transform import Rotation

    rclpy.init()
    node = rclpy.create_node("egohalo_stereo_rectifier")
    namespace = settings["namespace"]
    views = stereo_views(config)
    streams = {s["name"]: s for c in config["cameras"] for s in c.get("dashboard_streams", [])}
    if not views:
        views = [dict(name="front", left_source="egohalo_stereo_left", right_source="egohalo_stereo_right",
                      **{key: config["mapping"][key] for key in (
                          "left_image_topic", "right_image_topic", "left_info_topic", "right_info_topic",
                          "left_frame", "right_frame")})]
    sources = list(dict.fromkeys(v[side + "_source"] for v in views for side in ("left", "right")))
    buffer = Buffer()
    _listener = TransformListener(buffer, node)
    broadcaster = StaticTransformBroadcaster(node) if sink is None else sink
    if sink is not None:
        sink.attach(node, config)
    infos = {name: None for name in sources}
    calibration = [None]
    queues = {name: queue.Queue(maxsize=8) for name in sources}
    reset = {name: threading.Event() for name in sources}
    failed = threading.Event()
    metrics = {name: dict(received=0, queue_resets=0, reference_resets=0,
                          reference_gaps=0, timestamp_gaps=0, timestamp_rollbacks=0,
                          waiting_for_idr=0,
                          expired_frames=0,
                          decoded=0, selected=0, published={},
                          rectify_ms=0.0, message_ms=0.0, publish_ms=0.0) for name in sources}
    source_fps = float(settings.get("source_fps", 30.0))
    output_hz = float(settings["output_hz"])
    if not 0 < output_hz <= source_fps:
        raise ValueError("Rectification requires 0 < output_hz <= source_fps")
    max_gap_ns = int(1.5e9 / source_fps)
    sample_tolerance_ns = int(.54e9 / source_fps)
    publishers, info_publishers = {}, {}
    for view in views:
        for side in ("left", "right"):
            key = (view["name"], side)
            if sink is None:
                publishers[key] = node.create_publisher(Image, view[side + "_image_topic"], 2)
                info_publishers[key] = node.create_publisher(CameraInfo, view[side + "_info_topic"], 2)
    status_publisher = (node.create_publisher(String, f"/{namespace}/rig/status", 2)
                        if sink is None else sink)

    def configure():
        if any(info is None for info in infos.values()):
            return
        try:
            matrices = []
            for view in views:
                left, right = [infos[view[side + "_source"]] for side in ("left", "right")]
                transform = buffer.lookup_transform(right.header.frame_id, left.header.frame_id, Time()).transform
                matrices.append(matrix_from_transform(
                    (transform.translation.x, transform.translation.y, transform.translation.z),
                    (transform.rotation.x, transform.rotation.y, transform.rotation.z, transform.rotation.w)))
            for name in sources:
                transform = buffer.lookup_transform(config["mapping"]["imu_frame"], infos[name].header.frame_id, Time()).transform
                matrices.append(matrix_from_transform(
                    (transform.translation.x, transform.translation.y, transform.translation.z),
                    (transform.rotation.x, transform.rotation.y, transform.rotation.z, transform.rotation.w)))
            identity = calibration_identity([infos[name] for name in sources], matrices,
                rectification={key: settings[key] for key in (
                    "output_width", "output_height", "horizontal_fov_deg") if key in settings})
            if calibration[0] is not None and calibration[0][0] == identity:
                return
            routes = {name: [] for name in sources}
            transforms = []
            baselines = {}
            for view, matrix in zip(views, matrices):
                pair_infos = [infos[view[side + "_source"]] for side in ("left", "right")]
                value = FisheyeRectification.create(
                    *pair_infos, matrix, size=(settings["output_width"], settings["output_height"]),
                    horizontal_fov_deg=float(settings.get("horizontal_fov_deg", 90.0)))
                baselines[view["name"]] = float(np.linalg.norm(matrix[:3, 3]))
                for index, side in enumerate(("left", "right")):
                    # Immutable frame identities keep delayed images tied to their calibration.
                    frame = view[side + "_frame"] + "_" + identity
                    transform = TransformStamped()
                    transform.header.stamp = node.get_clock().now().to_msg()
                    transform.header.frame_id = pair_infos[index].header.frame_id
                    transform.child_frame_id = frame
                    quat = Rotation.from_matrix(value.rotations[index].T).as_quat()
                    transform.transform.rotation.x = float(quat[0])
                    transform.transform.rotation.y = float(quat[1])
                    transform.transform.rotation.z = float(quat[2])
                    transform.transform.rotation.w = float(quat[3])
                    transforms.append(transform)
                    routes[view[side + "_source"]].append((view["name"], side, index, frame, value))
            broadcaster.sendTransform(transforms)
            calibration[0] = (identity, routes)
            status_publisher.publish(String(data=json.dumps(dict(
                calibration_id=identity, sources=sources, views=baselines, state="ready"))))
            node.get_logger().info(f"Fisheye rig ready: {identity}, {len(sources)} sources, {len(views)} pairs")
        except Exception as exc:
            # Stop publishing geometry until the entire rig is valid again.
            calibration[0] = None
            node.get_logger().warning(f"Waiting for valid rig calibration/TF: {exc}", throttle_duration_sec=5)

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
            def make_decoder():
                return H264Decoder(hardware=settings.get("hardware_decode", True),
                    pixel_format="GRAY8", low_latency=True, frame_selector=selected)
            decoder = make_decoder()
            while not stop.is_set():
                try:
                    message, received = queues[index].get(timeout=.1)
                except queue.Empty:
                    continue
                stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
                data = bytes(message.data)
                contiguous = sequence.observe(data)
                metrics[index]["reference_gaps"] += int(contiguous is False)
                metrics[index]["timestamp_gaps"] += int(last_input is not None and stamp - last_input > max_gap_ns)
                metrics[index]["timestamp_rollbacks"] += int(last_input is not None and stamp <= last_input)
                capture_gap = contiguous is True and last_input is not None and 0 < stamp - last_input < min(8, sequence.modulus - 1) * 1e9 / source_fps
                if reset[index].is_set() or contiguous is False or (last_input is not None and not 0 < stamp-last_input <= max_gap_ns and not capture_gap):
                    metrics[index]["reference_resets"] += 1
                    decoder.gate.reset()
                    if last_input is not None and stamp <= last_input:
                        decoder.close()
                        decoder = make_decoder()
                        headers.clear()
                        last_output = None
                        if sink is not None:
                            sink.reset_frames()
                    reset[index].clear()
                last_input = stamp
                if decoder.push(data, stamp):
                    headers[stamp] = (copy.deepcopy(message.header), received)
                else:
                    metrics[index]["waiting_for_idr"] += 1
                for _ in range(16):
                    # Most decoded frames are rejected; do not wait for an
                    # appsink sample on every compressed input packet.
                    result = decoder.pull(0, select_frame=selected)
                    if result is None:
                        break
                    pts, image = result
                    metadata = headers.pop(pts, None)
                    value = calibration[0]
                    if metadata is None or value is None or image is None:
                        continue
                    header, frame_received = metadata
                    if time.monotonic() - frame_received > float(settings.get("max_output_age_sec", .5)):
                        metrics[index]["expired_frames"] += 1
                        continue
                    bucket = int(round(pts / period))
                    # Both eyes select the same exposure using the shared source clock.
                    if bucket == last_output or abs(pts - bucket * period) > sample_tolerance_ns:
                        continue
                    last_output = bucket
                    for name, side, eye, frame, rectification in value[1][index]:
                        started = time.perf_counter()
                        rectified = rectification.rectify(eye, image)
                        metrics[index]["rectify_ms"] += (time.perf_counter() - started) * 1000
                        started = time.perf_counter()
                        output = Image()
                        out_header = copy.deepcopy(header)
                        out_header.frame_id = frame
                        output.header = out_header
                        output.height, output.width = rectified.shape
                        output.encoding = "mono8"
                        output.step = output.width
                        # The ROS setter validates bytes element by element;
                        # a typed byte array takes its validated bulk path.
                        output.data = array("B", rectified.tobytes())
                        info = CameraInfo()
                        info.header = out_header
                        info.width, info.height = rectification.size
                        info.distortion_model = "plumb_bob"
                        info.d = [0.0] * 5
                        info.k = rectification.projections[eye][:, :3].flatten().tolist()
                        info.r = np.eye(3).flatten().tolist()
                        info.p = rectification.projections[eye].flatten().tolist()
                        metrics[index]["message_ms"] += (time.perf_counter() - started) * 1000
                        started = time.perf_counter()
                        if sink is None:
                            info_publishers[(name, side)].publish(info)
                            publishers[(name, side)].publish(output)
                        else:
                            sink.publish_frame(name, side, output, info)
                        metrics[index]["publish_ms"] += (time.perf_counter() - started) * 1000
                        key = name + "/" + side
                        published = metrics[index]["published"]
                        published[key] = published.get(key, 0) + 1
                for discarded in decoder.take_discarded_timestamps():
                    headers.pop(discarded, None)
                metrics[index]["decoded"] = decoder.decoded_frames
                metrics[index]["selected"] = decoder.selected_frames
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
    for index in sources:
        def on_info(message, index=index):
            infos[index] = message
        def on_image(message, index=index):
            if message.format.lower() != "h264":
                return
            metrics[index]["received"] += 1
            try:
                queues[index].put_nowait((message, time.monotonic()))
            except queue.Full:
                metrics[index]["queue_resets"] += 1
                reset[index].set()
                while True:
                    try:
                        queues[index].get_nowait()
                    except queue.Empty:
                        break
        node.create_subscription(CameraInfo, streams[index]["camera_info_topic"], on_info, qos_profile_sensor_data)
        node.create_subscription(CompressedImage, streams[index]["topic"], on_image, 8)
        thread = threading.Thread(target=worker, args=(index,), daemon=True)
        thread.start()
        threads.append(thread)
    node.create_timer(.5, configure)
    def publish_metrics():
        value = calibration[0]
        status_publisher.publish(String(data=json.dumps(dict(
            state="ready" if value is not None else "waiting_calibration",
            calibration_id=value[0] if value is not None else None,
            opencv_threads=cv2.getNumThreads(), sources=metrics,
            transport="direct_source" if sink is not None else "relayed",
            **({"bridge": sink.stats()} if sink is not None else {}),
        ))))
    node.create_timer(1.0, publish_metrics)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if config["egohalo_rectification"].get("direct_source", False):
        from .egohalo_mapping_bridge import run_bridge
        run_bridge(config)
    else:
        run_rectifier(config, threading.Event())


if __name__ == "__main__":
    main()
