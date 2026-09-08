"""ROS head input, optional USB/ROS RGB and line-based serial widths."""

import json
import copy
import os
import select
import subprocess
import threading
import time

import numpy as np

from insight_capture.runtime.mapping.geometry import PoseSample, matrix_from_transform
from .devices import local_device, scan_devices, valid_device
from .capture import write_json


def start_sources(capture):
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rclpy.time import Time
    from sensor_msgs.msg import CameraInfo, CompressedImage
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

    last_vio_received = None

    def vio(message):
        nonlocal last_vio_received
        p, q = message.pose.position, message.pose.orientation
        position, quaternion = [p.x, p.y, p.z], [q.x, q.y, q.z, q.w]
        if (
            not np.isfinite(position + quaternion).all()
            or np.linalg.norm(quaternion) < 1e-8
        ):
            capture.source_state("vio/head", "invalid", "无效 VIO 数值")
            return
        now = time.monotonic()
        if last_vio_received is not None and now - last_vio_received > 2:
            capture.pose.generation += 1
            capture.pose.buffer.clear()
            capture.pose.previous.clear()
        last_vio_received = now
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
            capture.source_state(
                "calibration",
                "invalid",
                "头部输入须为已校正图像；请使用 image_rect_raw",
            )
            return
        if intrinsic[0, 0] > 0 and intrinsic[1, 1] > 0 and np.isfinite(intrinsic).all():
            capture.source_state("calibration", "ready")
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

    node.create_subscription(
        CompressedImage,
        head["image_topic"],
        image_callback("head", True),
        qos_profile_sensor_data,
    )
    capture.sources = SourceManager(capture, node, image_callback)
    capture.sources.start()

    def spin():
        from rclpy.executors import ExternalShutdownException

        try:
            rclpy.spin(node)
        except ExternalShutdownException:
            pass

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    return node, listener, [thread]


class SourceManager:
    def __init__(self, capture, node=None, image_callback=None):
        self.capture, self.node, self.image_callback = capture, node, image_callback
        self.handles = []

    def start(self):
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CompressedImage, Image

        for item in self.capture.config.get("rgb", []):
            stop = threading.Event()
            if "topic" in item:
                compressed = item.get(
                    "compressed", item["topic"].endswith("/compressed")
                )
                callback = self.image_callback(item["name"], compressed)

                def receive(message, callback=callback, stop=stop):
                    if not stop.is_set():
                        callback(message)

                subscription = self.node.create_subscription(
                    CompressedImage if compressed else Image,
                    item["topic"],
                    receive,
                    qos_profile_sensor_data,
                )
                self.handles.append((stop, None, subscription))
            elif "device" in item:
                thread = threading.Thread(
                    target=usb_worker, args=(self.capture, item, stop), daemon=True
                )
                self.handles.append((stop, thread, None))
                thread.start()
        for index, item in enumerate(self.capture.config.get("serial", [])):
            stop = threading.Event()
            thread = threading.Thread(
                target=serial_worker,
                args=(self.capture, item, stop, index),
                daemon=True,
            )
            self.handles.append((stop, thread, None))
            thread.start()

    def close(self):
        for stop, _, _ in self.handles:
            stop.set()
        for _, thread, subscription in self.handles:
            if thread:
                thread.join(4)
                if thread.is_alive():
                    raise ValueError("设备读取尚未退出，请稍后重试")
            if subscription is not None:
                self.node.destroy_subscription(subscription)
        self.handles.clear()

    def apply(self, value):
        capture = self.capture
        with capture.lock:
            if capture.active or capture.draining or capture.reconfiguring:
                raise ValueError("请先停止录制，再选择设备")
            capture.reconfiguring = True
        old = copy.deepcopy(
            {key: capture.config.get(key, []) for key in ("rgb", "serial")}
        )
        try:
            settings = validate_bindings(value, old)
            self.close()
            capture.queue.join()
            with capture.lock:
                capture.config.update(settings)
                for key in list(capture.source_states):
                    if key not in ("image/head", "vio/head", "calibration"):
                        capture.source_states.pop(key, None)
                capture.grippers.clear()
                for key in list(capture.latest):
                    if key != "image/head" and key != "vio/head":
                        capture.latest.pop(key, None)
                        capture.arrivals.pop(key, None)
                for key in list(capture.preview):
                    if key != "head":
                        capture.preview.pop(key, None)
            try:
                self.start()
                write_json(capture.root / "_devices.json", settings)
            except Exception:
                self.close()
                capture.config.update(old)
                self.start()
                raise
            return settings
        finally:
            with capture.lock:
                capture.reconfiguring = False


def validate_bindings(value, current):
    if not isinstance(value, dict) or set(value) != {"rgb", "serial"}:
        raise ValueError("设备配置须包含 rgb 和 serial")
    if not isinstance(value["rgb"], list) or not isinstance(value["serial"], list):
        raise ValueError("设备配置须为列表")
    if any(not isinstance(x, dict) for key in ("rgb", "serial") for x in value[key]):
        raise ValueError("无效设备配置")
    if {x.get("name") for x in value["rgb"]} != {
        x["name"] for x in current["rgb"]
    } or len(value["rgb"]) != len(current["rgb"]):
        raise ValueError("请保留已配置的 RGB 通道名称")
    if len(value["serial"]) > 2:
        raise ValueError("首版最多配置两个编码器串口")
    result = copy.deepcopy(value)
    used = set()
    assigned_hands = set()
    available = scan_devices()
    for kind, items in (("video", result["rgb"]), ("serial", result["serial"])):
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("无效设备配置")
            if "device" in item:
                if not valid_device(item["device"], kind):
                    raise ValueError("请选择有效的设备端口")
                item["device"] = next(
                    (
                        x["device"]
                        for x in available[kind]
                        if x["node"] == item["device"]
                    ),
                    item["device"],
                )
                identity = os.path.realpath(local_device(item["device"]))
                if identity in used:
                    raise ValueError("同一个设备不能重复绑定")
                used.add(identity)
                if kind == "video":
                    item.pop("topic", None)
                    item.pop("compressed", None)
            elif (
                kind == "video"
                and isinstance(item.get("topic"), str)
                and item["topic"].startswith("/")
            ):
                pass
            else:
                raise ValueError("请选择设备或保留 ROS 话题")
            if kind == "serial":
                baudrate = int(item.get("baudrate", 115200))
                if not 300 <= baudrate <= 4000000:
                    raise ValueError("串口波特率超出范围")
                item["baudrate"] = baudrate
                if item.get("hands", "both") not in ("left", "right", "both"):
                    raise ValueError("请选择参与的编码器通道")
                hands = (
                    {"left", "right"}
                    if item.get("hands", "both") == "both"
                    else {item["hands"]}
                )
                if assigned_hands & hands:
                    raise ValueError(
                        "两个串口须分别选择左手和右手，不能重复绑定编码器通道"
                    )
                assigned_hands.update(hands)
            for key in ("fps", "width", "height"):
                if key in item and not 1 <= int(item[key]) <= (
                    120 if key == "fps" else 8192
                ):
                    raise ValueError("相机分辨率或帧率超出范围")
    return result


def stopped(capture, stop):
    return capture.stop_event.is_set() or stop.is_set()


def stable_path(item, kind):
    value = item["device"]
    value = f"/dev/video{value}" if isinstance(value, int) else value
    if "/by-" not in value:
        value = next(
            (x["device"] for x in scan_devices()[kind] if x["node"] == value), value
        )
    return value


def usb_worker(capture, item, stop=None):
    stop = stop or capture.stop_event
    key = "image/" + item["name"]
    device = stable_path(item, "video")
    while not stopped(capture, stop):
        if "/by-" not in device:
            device = stable_path({"device": device}, "video")
        process = None
        try:
            capture.source_state(key, "connecting", "等待相机，自动重连中", device)
            path = local_device(device)
            if not os.path.exists(path):
                raise OSError("相机未连接")
            # A child process bounds V4L2 driver stalls as well as unplug failures.
            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-f",
                "v4l2",
            ]
            if "fps" in item:
                args += ["-framerate", str(item["fps"])]
            if "width" in item and "height" in item:
                args += ["-video_size", f"{item['width']}x{item['height']}"]
            args += [
                "-i",
                path,
                "-an",
                "-threads",
                "1",
                "-c:v",
                "mjpeg",
                "-q:v",
                "3",
                "-flush_packets",
                "1",
                "-f",
                "image2pipe",
                "pipe:1",
            ]
            process = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            data = bytearray()
            last_frame = time.monotonic()
            while not stopped(capture, stop):
                if time.monotonic() - last_frame > 4:
                    raise OSError("相机 4 秒无图像，请检查设备占用或采集格式")
                ready, _, _ = select.select([process.stdout], [], [], 0.2)
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise OSError("相机断开或无法读取，请检查权限、占用和采集格式")
                data.extend(chunk)
                if len(data) > 32 * 1024 * 1024:
                    raise ValueError("相机图像超过缓冲上限")
                while (end := data.find(b"\xff\xd9")) >= 0:
                    start = data.find(b"\xff\xd8")
                    jpeg = bytes(data[start : end + 2]) if 0 <= start < end else None
                    del data[: end + 2]
                    if jpeg and not stopped(capture, stop):
                        last_frame = time.monotonic()
                        capture.source_state(key, "ready", "", device)
                        capture.submit("image", item["name"], 0, jpeg, last_frame)
        except (OSError, ValueError) as exc:
            if not stopped(capture, stop):
                capture.source_state(key, "disconnected", str(exc), device)
        finally:
            if process:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                process.stdout.close()
        stop.wait(1)


def serial_worker(capture, item, stop=None, index=0):
    import serial

    stop = stop or capture.stop_event
    key = f"serial/{index}"
    device = stable_path(item, "serial")
    while not stopped(capture, stop):
        if "/by-" not in device:
            device = stable_path({"device": device}, "serial")
        try:
            capture.source_state(key, "connecting", "等待编码器，自动重连中", device)
            with serial.Serial(
                local_device(device),
                item.get("baudrate", 115200),
                timeout=0.2,
                exclusive=True,
            ) as port:
                data = bytearray()
                last_data = time.monotonic()
                while not stopped(capture, stop):
                    raw = port.read(max(1, min(port.in_waiting, 4096)))
                    if not raw:
                        if time.monotonic() - last_data > 4:
                            raise OSError("串口 4 秒无数据，正在重新连接")
                        continue
                    last_data = time.monotonic()
                    data.extend(raw)
                    if len(data) > 4096:
                        data.clear()
                        capture.source_state(
                            key, "invalid", "串口单行超过 4096 字节", device
                        )
                    while b"\n" in data:
                        line, _, rest = data.partition(b"\n")
                        data = bytearray(rest)
                        try:
                            values = json.loads(line)
                            if not isinstance(values, dict):
                                raise ValueError("串口须输出 JSON 对象")
                            count = 0
                            valid_all = True
                            for arm in ("left", "right"):
                                if arm not in values or item.get(
                                    "hands", "both"
                                ) not in (arm, "both"):
                                    continue
                                raw_value = float(values[arm])
                                calibration = item.get(arm, {})
                                width = raw_value * calibration.get(
                                    "scale_m", 1
                                ) + calibration.get("offset_m", 0)
                                valid = bool(
                                    np.isfinite(width)
                                    and 0
                                    <= width
                                    <= calibration.get("max_width_m", 0.1)
                                )
                                valid_all &= valid
                                count += 1
                                if not stopped(capture, stop):
                                    capture.submit(
                                        "gripper",
                                        arm,
                                        0,
                                        {
                                            "raw": raw_value
                                            if np.isfinite(raw_value)
                                            else None,
                                            "width_m": width if valid else None,
                                            "valid": valid,
                                        },
                                    )
                            if not count:
                                raise ValueError("未收到所选 left/right 编码器通道")
                            capture.source_state(
                                key,
                                "ready" if valid_all else "invalid",
                                "" if valid_all else "开合度超出标定范围",
                                device,
                            )
                        except (ValueError, TypeError, AttributeError) as exc:
                            capture.source_state(
                                key, "invalid", "串口格式错误: " + str(exc), device
                            )
        except (OSError, ValueError, serial.SerialException) as exc:
            if not stopped(capture, stop):
                capture.source_state(key, "disconnected", str(exc), device)
        stop.wait(1)
