"""ROS head input, optional USB/ROS RGB and line-based serial widths."""

import json
import threading
import time

import cv2
import numpy as np

from insight_capture.runtime.mapping.geometry import PoseSample, matrix_from_transform


def start_sources(capture):
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo, CompressedImage, Image
    from tf2_ros import Buffer, TransformListener

    from rclpy.signals import SignalHandlerOptions

    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = Node("aruco_capture")
    tf = Buffer()
    listener = TransformListener(tf, node)
    config = capture.config
    head = config["head"]

    def stamp(message):
        return message.header.stamp.sec * 10**9 + message.header.stamp.nanosec

    def vio(message):
        p, q = message.pose.position, message.pose.orientation
        position, quaternion = [p.x, p.y, p.z], [q.x, q.y, q.z, q.w]
        if (
            not np.isfinite(position + quaternion).all()
            or np.linalg.norm(quaternion) < 1e-8
        ):
            capture.error = "无效 VIO 数值"
            return
        capture.pose.add_vio(
            PoseSample(stamp(message), np.array(position), np.array(quaternion))
        )
        capture.submit(
            "vio",
            "head",
            stamp(message),
            {"position": position, "quaternion_xyzw": quaternion},
        )

    def info(message):
        intrinsic = (
            np.array(message.p).reshape(3, 4)[:, :3]
            if head.get("rectified", True)
            else np.array(message.k).reshape(3, 3)
        )
        if not head.get("rectified", True) and np.any(np.abs(message.d) > 1e-8):
            capture.error = "头部输入须为已校正图像；请使用 image_rect_raw"
            return
        if intrinsic[0, 0] > 0 and intrinsic[1, 1] > 0 and np.isfinite(intrinsic).all():
            capture.intrinsic = intrinsic
            capture.calibration = {
                "intrinsic": intrinsic.tolist(),
                "width": message.width,
                "height": message.height,
                "frame_id": message.header.frame_id,
            }

    def extrinsic():
        try:
            value = tf.lookup_transform(
                head["imu_frame"], head["rgb_frame"], Time()
            ).transform
            t, q = value.translation, value.rotation
            capture.imu_from_rgb = matrix_from_transform(
                [t.x, t.y, t.z], [q.x, q.y, q.z, q.w]
            )
        except Exception:
            pass

    node.create_subscription(
        PoseStamped, head["vio_topic"], vio, qos_profile_sensor_data
    )
    node.create_subscription(
        CameraInfo, head["info_topic"], info, qos_profile_sensor_data
    )
    node.create_timer(1, extrinsic)

    def image_callback(name, compressed):
        def receive(message):
            capture.submit(
                "image" if compressed else "raw_image",
                name,
                stamp(message),
                bytes(message.data) if compressed else message,
            )

        return receive

    workers = []
    for item in [{"name": "head", "topic": head["image_topic"]}] + config.get(
        "rgb", []
    ):
        if "topic" in item:
            compressed = item.get("compressed", item["topic"].endswith("/compressed"))
            node.create_subscription(
                CompressedImage if compressed else Image,
                item["topic"],
                image_callback(item["name"], compressed),
                qos_profile_sensor_data,
            )
        elif "device" in item:
            worker = threading.Thread(
                target=usb_worker, args=(capture, item), daemon=True
            )
            workers.append(worker)
            worker.start()
    for item in config.get("serial", []):
        worker = threading.Thread(
            target=serial_worker, args=(capture, item), daemon=True
        )
        workers.append(worker)
        worker.start()

    def spin():
        from rclpy.executors import ExternalShutdownException

        try:
            rclpy.spin(node)
        except ExternalShutdownException:
            pass

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    return node, listener, [thread, *workers]


def usb_worker(capture, item):
    video = cv2.VideoCapture(item["device"])
    try:
        for prop, key in [
            (cv2.CAP_PROP_FRAME_WIDTH, "width"),
            (cv2.CAP_PROP_FRAME_HEIGHT, "height"),
            (cv2.CAP_PROP_FPS, "fps"),
        ]:
            if key in item:
                video.set(prop, item[key])
        while not capture.stop_event.is_set():
            ok, frame = video.read()
            received = time.monotonic()
            if not ok:
                capture.error = "RGB 无数据: " + item["name"]
                break
            ok, jpeg = cv2.imencode(".jpg", frame)
            if ok:
                capture.submit("image", item["name"], 0, jpeg.tobytes(), received)
    finally:
        video.release()


def serial_worker(capture, item):
    import serial

    try:
        with serial.Serial(
            item["device"], item.get("baudrate", 115200), timeout=0.2
        ) as port:
            while not capture.stop_event.is_set():
                raw = port.readline(4096)
                if not raw:
                    continue
                try:
                    values = json.loads(raw)
                    for arm in ("left", "right"):
                        if arm not in values:
                            continue
                        raw_value = float(values[arm])
                        calibration = item.get(arm, {})
                        width = raw_value * calibration.get(
                            "scale_m", 1
                        ) + calibration.get("offset_m", 0)
                        valid = bool(
                            np.isfinite(width)
                            and 0 <= width <= calibration.get("max_width_m", 0.1)
                        )
                        capture.submit(
                            "gripper",
                            arm,
                            0,
                            {
                                "raw": raw_value if np.isfinite(raw_value) else None,
                                "width_m": width if valid else None,
                                "valid": valid,
                            },
                        )
                except (ValueError, TypeError, AttributeError):
                    capture.error = (
                        '串口格式错误，预期一行 JSON: {"left":数值,"right":数值}'
                    )
    except Exception as exc:
        capture.error = "串口: " + str(exc)
