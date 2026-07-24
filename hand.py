#!/usr/bin/env python3
"""Visualize camera hand landmarks on the compressed RGB stream."""

import argparse
import threading
import time
from collections import deque
from typing import Dict, Optional, Tuple

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from vision_msgs.msg import Detection2DArray

# MediaPipe hand connections, grouped by finger for coloring. (a, b, finger)
FINGER_COLORS = {
    "thumb": (0, 0, 255),     # red
    "index": (0, 165, 255),   # orange
    "middle": (0, 255, 255),  # yellow
    "ring": (0, 255, 0),      # green
    "pinky": (255, 0, 0),     # blue
    "palm": (200, 200, 200),  # gray
}
HAND_EDGES = [
    (0, 1, "thumb"), (1, 2, "thumb"), (2, 3, "thumb"), (3, 4, "thumb"),
    (0, 5, "palm"), (5, 6, "index"), (6, 7, "index"), (7, 8, "index"),
    (5, 9, "palm"), (9, 10, "middle"), (10, 11, "middle"), (11, 12, "middle"),
    (9, 13, "palm"), (13, 14, "ring"), (14, 15, "ring"), (15, 16, "ring"),
    (13, 17, "palm"), (0, 17, "palm"), (17, 18, "pinky"), (18, 19, "pinky"), (19, 20, "pinky"),
]


def stamp_to_ns(stamp) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


class HandLandmarkVisualizer(Node):
    def __init__(self, args) -> None:
        super().__init__("insight_hand_visualizer")
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
        )
        self._window = args.window_name
        self._min_score = args.min_score
        self._max_sync_delta_ns = int(args.max_sync_ms * 1_000_000)
        self._record_path = args.record
        self._max_width = args.max_width
        # Resizable window so the full frame is visible and the user can drag it.
        cv2.namedWindow(self._window, cv2.WINDOW_NORMAL)

        self._lock = threading.Lock()
        self._image_buffer: "deque[Tuple[int, np.ndarray]]" = deque()
        self._image_buffer_size = 12
        self._latest_hands: Optional[Detection2DArray] = None
        self._latest_kps: Optional[Detection2DArray] = None
        self._latest_kps_stamp_ns = 0
        self._recv_kps_ns = 0
        self._mode_timeout_ns = 1_500_000_000

        self._writer: Optional[cv2.VideoWriter] = None
        self._record_fps = args.video_fps
        self._frames = 0
        self._fps_ts = time.monotonic()
        self._fps = 0.0

        self.create_subscription(CompressedImage, args.image_topic, self._on_image, qos)
        self.create_subscription(Detection2DArray, args.hand_topic, self._on_hands, qos)
        self.create_subscription(Detection2DArray, args.keypoints_topic, self._on_kps, qos)
        self.create_timer(0.03, self._render)
        self.get_logger().info(
            f"hand visualizer started: image={args.image_topic} hand={args.hand_topic} "
            f"keypoints={args.keypoints_topic}"
        )

    def _on_image(self, msg: CompressedImage) -> None:
        arr = np.frombuffer(msg.data, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None:
            self.get_logger().warning("failed to decode compressed image")
            return
        with self._lock:
            self._image_buffer.append((stamp_to_ns(msg.header.stamp), image))
            while len(self._image_buffer) > self._image_buffer_size:
                self._image_buffer.popleft()

    def _on_hands(self, msg: Detection2DArray) -> None:
        with self._lock:
            self._latest_hands = msg

    def _on_kps(self, msg: Detection2DArray) -> None:
        with self._lock:
            self._latest_kps = msg
            self._latest_kps_stamp_ns = stamp_to_ns(msg.header.stamp)
            self._recv_kps_ns = time.monotonic_ns()

    def _pick_image(self, target_stamp_ns: int) -> Optional[Tuple[int, np.ndarray]]:
        """Image whose stamp is closest to the keypoints stamp, else latest."""
        if not self._image_buffer:
            return None
        if target_stamp_ns <= 0:
            return self._image_buffer[-1]
        best = min(self._image_buffer, key=lambda it: abs(it[0] - target_stamp_ns))
        return best

    def _render(self) -> None:
        with self._lock:
            now_ns = time.monotonic_ns()
            hands = self._latest_hands
            kps = self._latest_kps
            kps_stamp_ns = self._latest_kps_stamp_ns
            active = kps is not None and (now_ns - self._recv_kps_ns) <= self._mode_timeout_ns
            image_item = self._pick_image(kps_stamp_ns if active else 0)

        if image_item is None:
            return
        image_stamp_ns, base = image_item
        frame = base.copy()

        hand_count = 0
        kpt_age_ms = 0.0
        if active and kps is not None:
            kpt_age_ms = (image_stamp_ns - kps_stamp_ns) / 1_000_000.0
            in_sync = abs(image_stamp_ns - kps_stamp_ns) <= self._max_sync_delta_ns
            if in_sync:
                hand_count = self._draw(frame, hands, kps)
            else:
                cv2.putText(frame, f"kpt out of sync ({kpt_age_ms:.0f} ms)", (16, 64),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
        else:
            cv2.putText(frame, "waiting for hand landmarks", (16, 64),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)

        self._update_fps()
        cv2.putText(frame, f"hands={hand_count}  kpt_age_ms={kpt_age_ms:.1f}  fps={self._fps:.1f}",
                    (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)

        if self._record_path:
            self._write_video(frame)

        # Downscale only for display so a full-res stream fits on screen.
        display = frame
        if self._max_width > 0 and frame.shape[1] > self._max_width:
            scale = self._max_width / frame.shape[1]
            display = cv2.resize(
                frame,
                (self._max_width, max(1, int(round(frame.shape[0] * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        cv2.imshow(self._window, display)
        if (cv2.waitKey(1) & 0xFF) in (27, ord("q")):
            self.get_logger().info("quit requested")
            rclpy.shutdown()

    def _draw(self, frame: np.ndarray, hands: Optional[Detection2DArray],
              kps: Detection2DArray) -> int:
        # group keypoints by hand index -> {kp_idx: (x, y, score)}
        grouped: Dict[int, Dict[int, Tuple[int, int, float]]] = {}
        for det in kps.detections:
            hi, ki = self._parse_id(det.id)
            if hi is None or ki is None:
                continue
            score = det.results[0].hypothesis.score if det.results else 0.0
            if score < self._min_score:
                continue
            x = int(det.bbox.center.position.x)
            y = int(det.bbox.center.position.y)
            grouped.setdefault(hi, {})[ki] = (x, y, float(score))

        # hand-index -> (label, score, box) from the hand message
        boxes: Dict[int, Tuple[str, float, Tuple[int, int, int, int]]] = {}
        if hands is not None:
            for det in hands.detections:
                try:
                    hi = int(det.id)
                except (ValueError, TypeError):
                    continue
                label, score = self._best(det)
                w = float(det.bbox.size_x)
                h = float(det.bbox.size_y)
                x = int(det.bbox.center.position.x - w * 0.5)
                y = int(det.bbox.center.position.y - h * 0.5)
                boxes[hi] = (label, score, (x, y, int(w), int(h)))

        for hi, points in grouped.items():
            # bones
            for a, b, finger in HAND_EDGES:
                if a in points and b in points:
                    cv2.line(frame, points[a][:2], points[b][:2],
                             FINGER_COLORS[finger], 2, cv2.LINE_AA)
            # joints
            for x, y, score in points.values():
                cv2.circle(frame, (x, y), 4 if score >= 0.5 else 3, (255, 255, 255), -1, cv2.LINE_AA)
                cv2.circle(frame, (x, y), 4 if score >= 0.5 else 3, (0, 0, 0), 1, cv2.LINE_AA)
            # box + label
            if hi in boxes:
                label, score, (bx, by, bw, bh) = boxes[hi]
                cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), (255, 128, 0), 1, cv2.LINE_AA)
                cv2.putText(frame, f"{label} {score:.2f}", (bx, max(18, by - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 128, 0), 2, cv2.LINE_AA)
        return len(grouped)

    def _update_fps(self) -> None:
        self._frames += 1
        now = time.monotonic()
        if now - self._fps_ts >= 1.0:
            self._fps = self._frames / (now - self._fps_ts)
            self._frames = 0
            self._fps_ts = now

    def _write_video(self, frame: np.ndarray) -> None:
        if self._writer is None:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(
                self._record_path, fourcc, self._record_fps, (frame.shape[1], frame.shape[0]))
            if not self._writer.isOpened():
                self.get_logger().error(f"failed to open video writer: {self._record_path}")
                self._record_path = ""
                self._writer = None
                return
            self.get_logger().info(f"recording to {self._record_path}")
        self._writer.write(frame)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None

    @staticmethod
    def _parse_id(value: str) -> Tuple[Optional[int], Optional[int]]:
        parts = value.split(":", 1)
        if len(parts) != 2:
            return (None, None)
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            return (None, None)

    @staticmethod
    def _best(det) -> Tuple[str, float]:
        if not det.results:
            return ("hand", 0.0)
        best = max(det.results, key=lambda r: float(r.hypothesis.score))
        return (best.hypothesis.class_id or "hand", float(best.hypothesis.score))


def main() -> None:
    parser = argparse.ArgumentParser(description="Visualize insight_full hand landmarks on RGB.")
    parser.add_argument("--image-topic", default="/camera/camera/color/image_rect_raw/compressed")
    parser.add_argument("--hand-topic", default="/camera/camera/hand")
    parser.add_argument("--keypoints-topic", default="/camera/camera/hand_keypoints")
    parser.add_argument("--window-name", default="insight_hand_landmarks")
    parser.add_argument("--min-score", type=float, default=0.3,
                        help="drop keypoints below this hand score")
    parser.add_argument("--max-sync-ms", type=float, default=120.0,
                        help="max |image - keypoints| stamp delta to overlay")
    parser.add_argument("--max-width", type=int, default=1280,
                        help="downscale display so width <= this (0 = native size)")
    parser.add_argument("--record", default="", help="optional output mp4 path")
    parser.add_argument("--video-fps", type=float, default=30.0)
    args = parser.parse_args()

    rclpy.init()
    node = HandLandmarkVisualizer(args)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        node.destroy_node()
        cv2.destroyAllWindows()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
