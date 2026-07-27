#!/usr/bin/env python3

"""Parse, stabilize, and render camera-published hand landmarks."""

import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import cv2
import numpy as np

from perf_tracker import track

# MediaPipe 21-point hand skeleton grouped by finger.
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

# Drawing threshold; raw API payloads retain all keypoints.
MIN_KEYPOINT_SCORE = 0.3

# Mark the API overlay stale after this interval.
HAND_DATA_TIMEOUT_SEC = 2.0

# Overlay freshness uses local receive time because source clocks differ.
MAX_SYNC_SEC = 0.12

# Single mapping point between HandEngine labels and dashboard roles.
HAND_CLASS_TO_ROLE = {
    "hand_left": "left_hand",
    "hand_right": "right_hand",
}

# Seed roles by arrival order; positional continuity keeps them stable afterward.
HAND_ROLE_SEED_ORDER = ("right_hand", "left_hand")

# Smooth unstable handedness labels with vote hysteresis.
HAND_LABEL_VOTE_WINDOW = 15
HAND_LABEL_FLIP_FRACTION = 0.7
# Require sustained challenger votes before switching labels.
HAND_LABEL_FLIP_MIN_VOTES = 8
# Ignore near-random handedness votes.
HAND_LABEL_MIN_CONFIDENCE = 0.6
# Use a tight sticky radius before wider positional reassignment.
HAND_POSITION_JUMP_FACTOR = 6.0
HAND_POSITION_STICKY_FACTOR = 1.0
# Keep role anchors longer than rendered tracks to bridge detection gaps.
HAND_ROLE_ANCHOR_MAX_AGE_SEC = 3.0

# Require consecutive hits before showing a 3D hand track.
HAND_TRACK_BIRTH_MIN_HITS = 3
# Maximum gap that still counts toward track birth.
HAND_TRACK_BIRTH_MAX_GAP_SEC = 0.30
# Hold the last shape briefly across missed detections.
HAND_TRACK_COAST_SEC = 0.5


@dataclass
class HandRoleTrack:
    """Lifecycle state for one teleop role's 3D hand skeleton."""

    streak: int = 0                # consecutive-hit count while unborn
    last_hit_monotonic: float = 0.0
    visible: bool = False
    landmarks: Optional[List[Optional[List[float]]]] = None


def smooth_hand_label(votes: Deque[str], current: Optional[str]) -> Optional[str]:
    """Apply vote hysteresis to the current handedness label."""
    if not votes:
        return current
    if current is None:
        return votes[-1]
    counts = Counter(votes)
    challenger, challenger_votes = max(
        ((label, count) for label, count in counts.items() if label != current),
        default=(None, 0),
        key=lambda item: item[1],
    )
    if (
        challenger is not None
        and challenger_votes >= HAND_LABEL_FLIP_MIN_VOTES
        and challenger_votes >= HAND_LABEL_FLIP_FRACTION * len(votes)
    ):
        return challenger
    return current

# Cosmetic depth offsets for otherwise-flat 2D landmarks.
_LANDMARK_Z = [
    0.00,                       # 0 wrist
    0.06, 0.12, 0.18, 0.24,     # 1-4 thumb
    0.00, 0.03, 0.06, 0.09,     # 5-8 index
    0.00, 0.03, 0.06, 0.09,     # 9-12 middle
    0.00, 0.03, 0.06, 0.09,     # 13-16 ring
    0.00, 0.03, 0.06, 0.09,     # 17-20 pinky
]


def normalize_hand_landmarks(
    keypoints: List[Dict[str, object]], min_score: float = MIN_KEYPOINT_SCORE
) -> Optional[List[Optional[List[float]]]]:
    """Normalize 21 keypoints to a wrist-local 3D frame."""
    points: Dict[int, Tuple[float, float]] = {}
    for kp in keypoints:
        if float(kp.get("score", 0.0)) < min_score:
            continue
        points[int(kp["i"])] = (float(kp["x"]), float(kp["y"]))
    if 0 not in points or 9 not in points:
        return None
    origin_x, origin_y = points[0]
    ref_x = points[9][0] - origin_x
    ref_y = points[9][1] - origin_y
    ref_len = math.hypot(ref_x, ref_y)
    if ref_len < 1e-6:
        return None
    ux, uy = ref_x / ref_len, ref_y / ref_len
    vx, vy = -uy, ux
    landmarks: List[Optional[List[float]]] = []
    for index in range(21):
        point = points.get(index)
        if point is None:
            landmarks.append(None)
            continue
        dx = (point[0] - origin_x) / ref_len
        dy = (point[1] - origin_y) / ref_len
        along = dx * ux + dy * uy
        lateral = dx * vx + dy * vy
        landmarks.append([round(along, 3), round(lateral, 3), _LANDMARK_Z[index]])
    return landmarks


def _hand_entry_anchor(entry: Dict[str, object]) -> Optional[Tuple[float, float, float]]:
    """Return a pixel-space center and scale for positional role matching."""
    bbox = entry.get("bbox")
    if bbox:
        x, y, w, h = bbox
        return (x + w * 0.5, y + h * 0.5, max(float(w), float(h), 1.0))
    points = {
        kp["i"]: (kp["x"], kp["y"])
        for kp in entry.get("keypoints", [])
        if float(kp.get("score", 0.0)) >= MIN_KEYPOINT_SCORE
    }
    if 0 in points and 9 in points:
        wx, wy = points[0]
        mx, my = points[9]
        return (wx, wy, max(math.hypot(mx - wx, my - wy) * 4.0, 1.0))
    if points:
        xs = [p[0] for p in points.values()]
        ys = [p[1] for p in points.values()]
        return (sum(xs) / len(xs), sum(ys) / len(ys), 40.0)
    return None


def draw_hands_on_frame(
    frame: np.ndarray, hands: List[Dict[str, object]], min_score: float = MIN_KEYPOINT_SCORE
) -> int:
    """Draw parsed hand landmarks onto a BGR frame in place."""
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
    """Provide camera-keyed hand overlay and 3D landmark state."""

    def _configure_hand_overlay(self) -> None:
        self.hand_overlay_enabled: set = set()
        self.hand_overlay_available: set = set()
        self._hand_latest_boxes: Dict[str, object] = {}
        self.hand_latest_snapshot: Dict[str, HandOverlaySnapshot] = {}
        # Bounded handedness vote state per camera and detected hand.
        self._hand_label_votes: Dict[Tuple[str, int], Deque[str]] = {}
        self._hand_label_current: Dict[Tuple[str, int], str] = {}
        # Per-role 3D skeleton lifecycle state.
        self._hand_role_tracks: Dict[str, HandRoleTrack] = {}
        # Last positional anchor for each camera-role pair.
        self._hand_role_anchor: Dict[Tuple[str, str], Tuple[float, float, float, float]] = {}

    def _on_hand_boxes(self, camera_name: str, msg, is_live: bool = True) -> None:
        # Gate separate live and /bagplay subscriptions by playback mode.
        if is_live == self._playback_mode:
            return
        self.hand_overlay_available.add(camera_name)
        self._hand_latest_boxes[camera_name] = msg

    def _on_hand_keypoints(self, camera_name: str, msg, is_live: bool = True) -> None:
        if is_live == self._playback_mode:
            return
        self.hand_overlay_available.add(camera_name)
        # Always parse because snapshots also feed the 3D skeleton.
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
                raw_label = entry.get("label")
                raw_score = float(entry.get("score", 0.0))
                if isinstance(raw_label, str) and raw_label in HAND_CLASS_TO_ROLE:
                    key = (camera_name, hand_index)
                    votes = self._hand_label_votes.setdefault(
                        key, deque(maxlen=HAND_LABEL_VOTE_WINDOW)
                    )
                    if raw_score >= HAND_LABEL_MIN_CONFIDENCE:
                        votes.append(raw_label)
                    smoothed = smooth_hand_label(votes, self._hand_label_current.get(key))
                    self._hand_label_current[key] = smoothed
                    entry["label_raw"] = raw_label
                    entry["label"] = smoothed
                hands.append(entry)

            self.hand_latest_snapshot[camera_name] = HandOverlaySnapshot(
                stamp_ns=self._stamp_to_ns(msg.header.stamp),
                hands=hands,
            )
            gesture_handler = getattr(self, "_handle_hand_gesture_snapshot", None)
            if gesture_handler is not None:
                gesture_handler(camera_name, hands, is_live=is_live)

            # Update 3D tracks using positional role matching.
            for entry, role in self._assign_hand_roles(camera_name, hands, time.monotonic()):
                landmarks = normalize_hand_landmarks(entry.get("keypoints", []))
                if landmarks is None:
                    continue
                self._hand_track_hit(role, landmarks)

    def _assign_hand_roles(
        self, camera_name: str, hands: List[Dict[str, object]], now: float
    ) -> List[Tuple[Dict[str, object], str]]:
        """Assign roles by sticky, wider, then arrival-order positional matching."""
        candidates = [
            (entry, anchor)
            for entry in hands
            if (anchor := _hand_entry_anchor(entry)) is not None
        ]

        assignments: List[Tuple[Dict[str, object], str]] = []
        role_taken = set()
        idx_taken = set()

        def _apply(pairs):
            pairs.sort(key=lambda item: item[0])
            for dist, role, idx in pairs:
                if role in role_taken or idx in idx_taken:
                    continue
                role_taken.add(role)
                idx_taken.add(idx)
                assignments.append((candidates[idx][0], role))

        # Stage 1: preserve each role within the tight sticky radius.
        sticky_pairs = []
        for role in HAND_CLASS_TO_ROLE.values():
            prev = self._hand_role_anchor.get((camera_name, role))
            if prev is None or now - prev[3] > HAND_ROLE_ANCHOR_MAX_AGE_SEC:
                continue
            px, py, pscale, _ = prev
            best = None
            for idx, (_, (x, y, scale)) in enumerate(candidates):
                dist = math.hypot(x - px, y - py)
                if dist <= HAND_POSITION_STICKY_FACTOR * max(pscale, scale):
                    if best is None or dist < best[0]:
                        best = (dist, role, idx)
            if best is not None:
                sticky_pairs.append(best)
        _apply(sticky_pairs)

        # Stage 2: match unresolved roles within the wider movement radius.
        wide_pairs = []
        for role in HAND_CLASS_TO_ROLE.values():
            if role in role_taken:
                continue
            prev = self._hand_role_anchor.get((camera_name, role))
            if prev is None or now - prev[3] > HAND_ROLE_ANCHOR_MAX_AGE_SEC:
                continue
            px, py, pscale, _ = prev
            for idx, (_, (x, y, scale)) in enumerate(candidates):
                if idx in idx_taken:
                    continue
                dist = math.hypot(x - px, y - py)
                if dist <= HAND_POSITION_JUMP_FACTOR * max(pscale, scale):
                    wide_pairs.append((dist, role, idx))
        _apply(wide_pairs)

        # Stage 3: seed unmatched roles by stable arrival order.
        remaining = sorted(
            (idx for idx in range(len(candidates)) if idx not in idx_taken),
            key=lambda idx: candidates[idx][0].get("hand_index", idx),
        )
        for role in HAND_ROLE_SEED_ORDER:
            if role in role_taken or not remaining:
                continue
            idx = remaining.pop(0)
            role_taken.add(role)
            idx_taken.add(idx)
            assignments.append((candidates[idx][0], role))

        for entry, role in assignments:
            anchor = next(a for e, a in candidates if e is entry)
            self._hand_role_anchor[(camera_name, role)] = (anchor[0], anchor[1], anchor[2], now)
        return assignments

    def _hand_track_hit(
        self, role: str, landmarks: List[Optional[List[float]]]
    ) -> None:
        """Record a role hit and birth its track after enough consecutive hits."""
        now = time.monotonic()
        track = self._hand_role_tracks.setdefault(role, HandRoleTrack())
        if now - track.last_hit_monotonic > HAND_TRACK_BIRTH_MAX_GAP_SEC:
            # Restart only an unborn streak; visible tracks use coast timeout.
            track.streak = 0
        track.streak += 1
        track.last_hit_monotonic = now
        track.landmarks = landmarks
        if track.streak >= HAND_TRACK_BIRTH_MIN_HITS:
            track.visible = True

    def set_hand_overlay_enabled(self, camera_name: str, enabled: bool) -> None:
        if camera_name not in self.hand_overlay_available:
            raise ValueError(f"'{camera_name}' has not published any hand-landmark data yet")
        if enabled:
            self.hand_overlay_enabled.add(camera_name)
            ensure_worker = getattr(self, "ensure_hand_overlay_worker", None)
            if ensure_worker is not None:
                try:
                    ensure_worker()
                except Exception:
                    self.hand_overlay_enabled.discard(camera_name)
                    raise
        else:
            self.hand_overlay_enabled.discard(camera_name)
            if not self.hand_overlay_enabled:
                stop_worker = getattr(self, "stop_hand_overlay_worker", None)
                if stop_worker is not None:
                    stop_worker()

    def hand_landmarks_for_role(self, role: str) -> Optional[List[Optional[List[float]]]]:
        """Return normalized role landmarks while its lifecycle track is alive."""
        track = self._hand_role_tracks.get(role)
        if track is None or not track.visible:
            return None
        if time.monotonic() - track.last_hit_monotonic > HAND_TRACK_COAST_SEC:
            track.visible = False
            track.streak = 0
            track.landmarks = None
            return None
        return track.landmarks

    def compose_hand_overlay_jpeg(
        self, camera_name: str, jpeg_bytes: bytes, version: int = 0
    ) -> bool:
        """Dispatch a fresh overlay frame to the worker process."""
        snapshot = self.hand_latest_snapshot.get(camera_name)
        if snapshot is None:
            return False
        now = time.monotonic()
        if now - snapshot.received_monotonic > HAND_DATA_TIMEOUT_SEC or not snapshot.hands:
            return False
        # Compare freshness on the local receive clock.
        if now - snapshot.received_monotonic > MAX_SYNC_SEC:
            return False
        dispatch = getattr(self, "_dispatch_hand_overlay", None)
        if dispatch is not None:
            dispatch(camera_name, version, jpeg_bytes, snapshot.hands)
            return True
        return False
