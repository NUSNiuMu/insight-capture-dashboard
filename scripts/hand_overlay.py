#!/usr/bin/env python3

"""Relay hand-landmark detections for the web dashboard's optional overlay.

Detection runs on the camera device itself (HandEngine, e.g. insight9_a
publishing <namespace>/camera/hand + <namespace>/camera/hand_keypoints as
vision_msgs/Detection2DArray) -- this module only parses and buffers the two
topics per camera for the API to serve; no CV work happens on this side.
Message shape mirrors visualize_hand_landmarks.py (a PC-side OpenCV
reference tool that lives outside this repo -- not something to look for
under scripts/; this module is a browser-side port of its drawing logic):
  hand           : one Detection2D per hand, id=handIdx, bbox=tight box,
                   results[].hypothesis.class_id="hand_left"/"hand_right"
  hand_keypoints : one Detection2D per landmark, id="handIdx:kpIdx",
                   bbox.center = pixel location
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from perf_tracker import track

# MediaPipe 21-point hand skeleton, grouped by finger for coloring -- same
# topology and colors as FINGER_COLORS/HAND_EDGES in visualize_hand_landmarks.py
# (the PC-side OpenCV reference tool, lives outside this repo).
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

# Matches visualize_hand_landmarks.py's --min-score default: keypoints below
# this confidence are dropped before drawing (not before storing -- the raw
# /api/cameras/{name}/hand JSON keeps everything).
MIN_KEYPOINT_SCORE = 0.3

# If no fresh hand_keypoints message has arrived in this long, the overlay
# API reports stale=True so the frontend can fade/hide it instead of
# freezing a stale skeleton on screen.
HAND_DATA_TIMEOUT_SEC = 2.0

# Matches visualize_hand_landmarks.py's --max-sync-ms default: the maximum
# |image_stamp - keypoints_stamp| gap for compose_hand_overlay_jpeg to draw
# the skeleton onto that particular image frame. Deliberately much tighter
# than HAND_DATA_TIMEOUT_SEC above -- that one answers "has the topic gone
# dead", this one answers "would drawing this specific keypoints frame onto
# this specific image frame look visibly misaligned" (HandEngine and the
# image stream are two independent publishers; a moving hand can drift
# several pixels within even 100ms). The reference tool solves the same
# problem by picking whichever buffered image is closest to the keypoints'
# stamp and printing "kpt out of sync" instead of drawing; the dashboard
# has no image buffer to pick from (it draws onto whichever frame just
# arrived), so out-of-sync here means "skip the overlay for this frame"
# instead -- the plain passthrough JPEG a moment of no overlay, not a
# visibly wrong one.
MAX_SYNC_NS = 120_000_000


def draw_hands_on_frame(
    frame: np.ndarray, hands: List[Dict[str, object]], min_score: float = MIN_KEYPOINT_SCORE
) -> int:
    """Port of HandLandmarkVisualizer._draw (visualize_hand_landmarks.py),
    drawing directly onto `frame`'s pixels (BGR, mutated in place) instead of
    a PC-side cv2 window. Takes already-parsed hand_overlay_payload() data
    rather than raw Detection2DArray messages, since that parsing already
    happened once in HandOverlayMixin._on_hand_keypoints. Returns the number
    of hands drawn.
    """
    drawn = 0
    for hand in hands:
        points: Dict[int, Tuple[int, int, float]] = {}
        for kp in hand.get("keypoints", []):
            score = float(kp.get("score", 0.0))
            if score < min_score:
                continue
            points[int(kp["i"])] = (int(kp["x"]), int(kp["y"]), score)
        if not points:
            continue
        drawn += 1

        for a, b, finger in HAND_EDGES:
            if a in points and b in points:
                cv2.line(frame, points[a][:2], points[b][:2], FINGER_COLORS[finger], 2, cv2.LINE_AA)
        for x, y, score in points.values():
            radius = 4 if score >= 0.5 else 3
            cv2.circle(frame, (x, y), radius, (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(frame, (x, y), radius, (0, 0, 0), 1, cv2.LINE_AA)

        bbox = hand.get("bbox")
        if bbox:
            x, y, w, h = (int(round(v)) for v in bbox)
            cv2.rectangle(frame, (x, y), (x + w, y + h), (255, 128, 0), 1, cv2.LINE_AA)
            if hand.get("label"):
                text = f"{hand['label']} {float(hand.get('score', 0.0)):.2f}"
                cv2.putText(frame, text, (x, max(18, y - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 128, 0), 2, cv2.LINE_AA)
    return drawn


@dataclass
class HandOverlaySnapshot:
    stamp_ns: int
    hands: List[Dict[str, object]] = field(default_factory=list)
    received_monotonic: float = field(default_factory=time.monotonic)


def _parse_hand_kp_id(value: str) -> Tuple[Optional[int], Optional[int]]:
    parts = value.split(":", 1)
    if len(parts) != 2:
        return (None, None)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (None, None)


def _best_result(det) -> Tuple[str, float]:
    if not det.results:
        return ("hand", 0.0)
    best = max(det.results, key=lambda r: float(r.hypothesis.score))
    return (best.hypothesis.class_id or "hand", float(best.hypothesis.score))


class HandOverlayMixin:
    """Mixin providing live hand-landmark overlay data, keyed by camera name.

    Expected host attributes: self.cameras (iterable with .name/.namespace)
    and self._stamp_to_ns (PoseBridgeNode already defines this staticmethod).
    """

    def _configure_hand_overlay(self) -> None:
        self.hand_overlay_enabled: set = set()
        self.hand_overlay_available: set = set()
        self._hand_latest_boxes: Dict[str, object] = {}
        self.hand_latest_snapshot: Dict[str, HandOverlaySnapshot] = {}

    def _on_hand_boxes(self, camera_name: str, msg) -> None:
        # Live-only: HandEngine's device-side hand topics aren't remapped
        # during `ros2 bag play` (see bagplay_topic / PlaybackManager), so a
        # still-connected live camera would otherwise overlay live hand
        # landmarks onto replayed video frames. Simpler than giving hand
        # overlay its own shadow-topic split like images/poses: it's a
        # visual toggle, not the primary display, so it just goes inert for
        # the duration of playback instead.
        if self._playback_mode:
            return
        # Cheap regardless of the Settings toggle -- this is what makes the
        # "Hand landmark overlay" checkbox appear at all (hand_overlay_available
        # is data-driven, see build_settings_payload). The actual per-message
        # parsing below is skipped unless enabled, so a disabled camera pays
        # only this one set-add per message, not the full Detection2DArray walk.
        self.hand_overlay_available.add(camera_name)
        if camera_name not in self.hand_overlay_enabled:
            return
        self._hand_latest_boxes[camera_name] = msg

    def _on_hand_keypoints(self, camera_name: str, msg) -> None:
        if self._playback_mode:
            return
        self.hand_overlay_available.add(camera_name)
        if camera_name not in self.hand_overlay_enabled:
            return
        with track(f"hand_parse:{camera_name}"):
            boxes_by_hand: Dict[int, Dict[str, object]] = {}
            boxes_msg = self._hand_latest_boxes.get(camera_name)
            if boxes_msg is not None:
                for det in boxes_msg.detections:
                    try:
                        hand_index = int(det.id)
                    except (TypeError, ValueError):
                        continue
                    label, score = _best_result(det)
                    width = float(det.bbox.size_x)
                    height = float(det.bbox.size_y)
                    x = float(det.bbox.center.position.x - width * 0.5)
                    y = float(det.bbox.center.position.y - height * 0.5)
                    boxes_by_hand[hand_index] = {
                        "label": label,
                        "score": score,
                        "bbox": [x, y, width, height],
                    }

            grouped: Dict[int, List[Dict[str, object]]] = {}
            for det in msg.detections:
                hand_index, kp_index = _parse_hand_kp_id(det.id)
                if hand_index is None or kp_index is None:
                    continue
                score = float(det.results[0].hypothesis.score) if det.results else 0.0
                grouped.setdefault(hand_index, []).append(
                    {
                        "i": kp_index,
                        "x": float(det.bbox.center.position.x),
                        "y": float(det.bbox.center.position.y),
                        "score": score,
                    }
                )

            hands = []
            for hand_index, keypoints in grouped.items():
                entry: Dict[str, object] = {
                    "hand_index": hand_index,
                    "keypoints": sorted(keypoints, key=lambda kp: kp["i"]),
                }
                entry.update(boxes_by_hand.get(hand_index, {}))
                hands.append(entry)

            self.hand_latest_snapshot[camera_name] = HandOverlaySnapshot(
                stamp_ns=self._stamp_to_ns(msg.header.stamp),
                hands=hands,
            )

    def set_hand_overlay_enabled(self, camera_name: str, enabled: bool) -> None:
        if camera_name not in self.hand_overlay_available:
            raise ValueError(f"'{camera_name}' has not published any hand-landmark data yet")
        if enabled:
            self.hand_overlay_enabled.add(camera_name)
        else:
            self.hand_overlay_enabled.discard(camera_name)

    def hand_overlay_payload(self, camera_name: str) -> Optional[Dict[str, object]]:
        snapshot = self.hand_latest_snapshot.get(camera_name)
        if snapshot is None:
            return None
        age_sec = time.monotonic() - snapshot.received_monotonic
        return {
            "camera": camera_name,
            "stamp_ns": snapshot.stamp_ns,
            "stale": age_sec > HAND_DATA_TIMEOUT_SEC,
            "hands": snapshot.hands,
        }

    def compose_hand_overlay_jpeg(
        self, camera_name: str, jpeg_bytes: bytes, image_stamp_ns: int
    ) -> Optional[bytes]:
        """Decode -> draw -> re-encode, the same per-frame cost the reference
        PC tool pays (cv2.imshow/--record both display the drawn-on copy, not
        the raw stream). Only called when hand_overlay_enabled for this
        camera, so cameras without it on keep the cheap passthrough path in
        _encode_dashboard_frame.
        """
        payload = self.hand_overlay_payload(camera_name)
        if not payload or payload["stale"] or not payload["hands"]:
            return None
        if abs(image_stamp_ns - payload["stamp_ns"]) > MAX_SYNC_NS:
            return None
        # NVJPEG path: decode to BGRx, draw in place (cv2 primitives accept 4
        # channels), re-encode -- both JPEG passes on the hardware engine.
        # Any None along the way falls through to the cv2 path below.
        hw_jpeg = getattr(self, "_hw_jpeg", None)
        if hw_jpeg is not None:
            with track(f"hand_overlay_draw_hw:{camera_name}"):
                image = hw_jpeg.decode_jpeg_bgrx(camera_name, jpeg_bytes)
                if image is not None:
                    draw_hands_on_frame(image, payload["hands"])
                    encoded = hw_jpeg.encode_bgrx(camera_name, image, quality=90)
                    if encoded is not None:
                        return encoded
        with track(f"hand_overlay_draw:{camera_name}"):
            arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if image is None:
                return None
            draw_hands_on_frame(image, payload["hands"])
            ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if not ok:
                return None
            return encoded.tobytes()
