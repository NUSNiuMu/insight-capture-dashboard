"""Encode configured raw recording streams without changing localization inputs."""

import argparse
import json
import os
from pathlib import Path
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from insight_capture.media.h264_encode import H264Encoder
from insight_capture.runtime.ros.network import configure_ros_network


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/cameras.json"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    settings = config["recording_h264"]
    inputs = settings["input_topics"]
    if not inputs or len(set(inputs)) != len(inputs):
        raise ValueError("Recording encoder inputs must be nonempty and unique")
    bitrate = int(settings["bitrate_bps"])
    if bitrate <= 0:
        raise ValueError("Recording encoder bitrate must be positive")
    os.environ["ROS_DOMAIN_ID"] = str(config["ros_domain_id"])
    configure_ros_network(config)
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
    from sensor_msgs.msg import Image, CompressedImage, CameraInfo
    from std_msgs.msg import String

    rclpy.init()
    node = rclpy.create_node("recording_h264_encoder")
    qos = QoSProfile(depth=30, reliability=ReliabilityPolicy.RELIABLE)
    stop = threading.Event()
    errors = queue.Queue()
    keyframes = {topic: threading.Event() for topic in inputs}
    queues = {topic: queue.Queue(maxsize=16) for topic in inputs}
    metrics = {topic: dict(received=0, submitted=0, encoded=0, source_missing=0,
                          queue_high_watermark=0, latency_ms=0., bytes=0,
                          last_source_ns=0, last_encoded_ns=0) for topic in inputs}
    publishers = {topic: node.create_publisher(CompressedImage, topic + "/h264", qos) for topic in inputs}
    status = node.create_publisher(String, "/insight_capture/recording_h264/status", 2)
    infos = {}
    info_subscriptions = [node.create_subscription(
        CameraInfo, topic.rsplit("/", 1)[0] + "/camera_info",
        lambda message, topic=topic: infos.__setitem__(topic, message), qos_profile_sensor_data) for topic in inputs]
    while rclpy.ok() and len(infos) < len(inputs):
        rclpy.spin_once(node, timeout_sec=.2)
    encoders = {topic: H264Encoder(infos[topic].width, infos[topic].height, bitrate) for topic in inputs}
    for subscription in info_subscriptions:
        node.destroy_subscription(subscription)

    def receive(topic, message):
        if stop.is_set():
            return
        try:
            metrics[topic]["received"] += 1
            queues[topic].put_nowait((message, time.monotonic()))
            metrics[topic]["queue_high_watermark"] = max(
                metrics[topic]["queue_high_watermark"], queues[topic].qsize())
        except queue.Full:
            errors.put(f"{topic}: recording encoder input queue exceeded 16 frames")
            stop.set()

    def encode(topic):
        encoder = encoders[topic]
        pending = {}
        last_stamp = None
        try:
            while not stop.is_set():
                try:
                    message, received = queues[topic].get(timeout=.005)
                except queue.Empty:
                    message = None
                if message is not None:
                    stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
                    if message.encoding.lower() != "nv12" or message.step != message.width:
                        raise ValueError("Recording encoder requires packed NV12 images")
                    if (message.width, message.height) != encoder.shape:
                        raise ValueError("Image resolution changed during encoding")
                    if last_stamp is not None and stamp - last_stamp > 50_000_000:
                        metrics[topic]["source_missing"] += max(0, round((stamp-last_stamp)*30/1e9)-1)
                    last_stamp = stamp
                    metrics[topic]["last_source_ns"] = stamp
                    if len(pending) >= 32:
                        raise RuntimeError("Encoder exceeded 32 pending frame timestamps")
                    pending[stamp] = (message.header, received)
                    if keyframes[topic].is_set():
                        encoder.request_idr()
                        keyframes[topic].clear()
                    encoder.push(message.data, stamp)
                    metrics[topic]["submitted"] += 1
                output = encoder.pull()
                while output is not None:
                    stamp, data = output
                    header, received = pending.pop(stamp)
                    publishers[topic].publish(CompressedImage(header=header, format="h264", data=data))
                    metrics[topic]["encoded"] += 1
                    metrics[topic]["last_encoded_ns"] = stamp
                    metrics[topic]["bytes"] += len(data)
                    metrics[topic]["latency_ms"] = (time.monotonic()-received)*1000
                    output = encoder.pull()
        except Exception as exc:
            errors.put(f"{topic}: {exc}")
            stop.set()
        finally:
            encoder.close()

    subscriptions = [node.create_subscription(Image, topic,
                     lambda message, topic=topic: receive(topic, message), qos) for topic in inputs]
    workers = [threading.Thread(target=encode, args=(topic,), daemon=True) for topic in inputs]
    for worker in workers:
        worker.start()

    def report():
        status.publish(String(data=json.dumps({"codec": "nvv4l2h264enc", "bitrate_bps": bitrate,
                       "ok": not stop.is_set() and errors.empty(), "error": failure, "streams": metrics})))

    node.create_timer(1., report)
    class Control(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            ok = not stop.is_set() and all(m["encoded"] for m in metrics.values())
            if self.path == "/request_idr" and ok:
                for event in keyframes.values():
                    event.set()
            elif self.path == "/drain" and ok:
                targets = {topic: values["received"] for topic, values in metrics.items()}
                deadline = time.monotonic() + 2.
                while time.monotonic() < deadline and not stop.is_set():
                    if all(metrics[t]["encoded"] >= count for t, count in targets.items()):
                        break
                    time.sleep(.005)
                ok = not stop.is_set() and all(metrics[t]["encoded"] >= count for t, count in targets.items())
            else:
                ok = False
            body = json.dumps({"ok": bool(ok), "streams": metrics}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    control = ThreadingHTTPServer(("127.0.0.1", int(settings["control_port"])), Control)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    failure = None
    try:
        while rclpy.ok() and not stop.is_set():
            rclpy.spin_once(node, timeout_sec=.1)
        if not errors.empty():
            failure = errors.get()
            node.get_logger().error(failure)
            report()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        control.shutdown()
        control.server_close()
        for worker in workers:
            worker.join(timeout=5)
        node.destroy_node()
        rclpy.try_shutdown()
    if failure:
        raise RuntimeError(failure)


if __name__ == "__main__":
    main()
