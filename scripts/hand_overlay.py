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

import math
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

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

# Which dashboard teleop role each HandEngine class_id anchors to in the 3D
# scene. UNVERIFIED against real data: insight9_a is an outward-looking head
# camera, so whether its "hand_left" is the wearer's physical left hand (the
# insight7_b wrist pose) or mirrored depends on HandEngine's own convention.
# If skeletons show up on the wrong wrist during a real replay, swap the two
# values here -- this dict is the single flip point.
HAND_CLASS_TO_ROLE = {
    "hand_left": "left_hand",
    "hand_right": "right_hand",
}

# HandEngine's per-frame left/right classification of a single hand is not
# stable -- observed flip-flopping between hand_left/hand_right frame to
# frame, which made the 3D skeleton jump between the two wrist nodes. The
# effective label is smoothed by majority vote over a sliding window with
# hysteresis: it only flips once the challenger label holds this fraction
# of the recent votes, so a 50/50 jitter stays put on the incumbent.
HAND_LABEL_VOTE_WINDOW = 15
HAND_LABEL_FLIP_FRACTION = 0.7
# A flip also needs this many absolute challenger votes, so a short burst of
# one label while the window is still filling (message drops make runs) can't
# flip the incumbent -- ~0.5s of consistent evidence at HandEngine's 15Hz.
HAND_LABEL_FLIP_MIN_VOTES = 8
# MediaPipe's handedness score is P(predicted label), always >= 0.5 -- near
# that floor the model is essentially guessing (confirmed flip-flopping
# reported near score~0.5 in google/mediapipe#3047), so votes that unsure
# are excluded from the window instead of diluting the real signal.
HAND_LABEL_MIN_CONFIDENCE = 0.6
# How far (in multiples of hand size) a role's hand may move between
# messages and still count as the same physical hand for _assign_hand_roles.
# Generous on purpose -- it only needs to reject "this is obviously a
# different hand", not track precisely; the classifier fallback covers the
# rest. See _assign_hand_roles for why this replaces trusting class_id
# directly: two simultaneously visible hands can swap HandEngine's
# hand_index between messages (and MediaPipe is known to sometimes label
# both hands the same, google/mediapipe#3902), which made the 3D skeleton
# jump between physical hands even though each hand's own label was stable.
#
# This alone turned out to still oscillate in practice: with two hands
# ~2-3 hand-widths apart (a normal, not-especially-close pose), BOTH fall
# within HAND_POSITION_JUMP_FACTOR of BOTH roles' last position, so it's a
# pure greedy nearest-of-4-pairs match every frame with no preference for
# "what it was last frame" -- ordinary per-frame detection jitter can make
# the wrong pairing marginally closer than the right one, and just as
# easily flip back the frame after, i.e. continuous back-and-forth, not a
# one-off swap. HAND_POSITION_STICKY_FACTOR below is the fix: a much
# tighter radius, checked first and in isolation per role (not a joint
# nearest-of-N match), so a role that hasn't moved much keeps its hand
# without ever being offered the other hand as an alternative. Must stay
# comfortably smaller than a typical inter-hand gap (so the *other* hand
# can't also land inside it) and comfortably bigger than frame-to-frame
# jitter of a hand that's actually just sitting still.
HAND_POSITION_JUMP_FACTOR = 6.0
HAND_POSITION_STICKY_FACTOR = 1.0

# Per-role track lifecycle for the 3D skeleton (hand_landmarks_for_role).
# HandEngine's per-frame recall is well below 100%: with a hand steadily in
# view it still misses individual frames, and it publishes an *empty*
# detection array on those (not "no message"), so "render only frames with a
# fresh detection" made the 3D rig blink several times a second. The 2D
# image overlay deliberately does NOT use these tracks -- drawing held-over
# keypoints onto a newer image frame would be visibly misaligned (see
# MAX_SYNC_NS); a missing overlay frame there is invisible, not a blink.
#   birth: this many consecutive detections before the skeleton appears,
#          filtering single-frame false positives;
HAND_TRACK_BIRTH_MIN_HITS = 3
#   consecutive = gaps shorter than this between hits (~4 frame periods at
#          HandEngine's 15Hz, so one dropped frame doesn't restart the count);
HAND_TRACK_BIRTH_MAX_GAP_SEC = 0.30
#   coast: after birth, dropout frames keep showing the last-seen hand shape
#          (held, not extrapolated) up to this long before the track dies.
HAND_TRACK_COAST_SEC = 0.5


@dataclass
class HandRoleTrack:
    """Lifecycle state for one teleop role's 3D hand skeleton."""

    streak: int = 0                # consecutive-hit count while unborn
    last_hit_monotonic: float = 0.0
    visible: bool = False
    landmarks: Optional[List[Optional[List[float]]]] = None


def smooth_hand_label(votes: Deque[str], current: Optional[str]) -> Optional[str]:
    """Return the smoothed label given the raw-vote history and the current
    smoothed label (None on first sight -> adopt the newest raw vote)."""
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

# Synthetic per-landmark depth (hand-plane normal) offsets, in the normalized
# units of normalize_hand_landmarks (wrist->middle-MCP distance == 1). The 2D
# detections carry no depth at all; these constants only keep the rendered
# hand from being perfectly flat -- thumb arcs out of the palm plane, finger
# joints lift slightly toward the fingertips. Purely cosmetic, tune freely.
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
    """Convert 21 pixel-space keypoints into a wrist-local normalized frame
    for the 3D scene: origin at landmark 0 (wrist), scaled so the wrist->
    middle-MCP (landmark 9) distance is 1 (cancels how close the hand is to
    the detecting camera's lens), axes a=along-hand toward the fingers,
    b=lateral in the hand plane, z=synthetic depth from _LANDMARK_Z. Returns
    a 21-entry list ([a, b, z] or None for below-score landmarks), or None
    when the two anchor landmarks aren't confidently detected.
    """
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
    """(center_x, center_y, hand_scale) in pixel space for _assign_hand_roles'
    positional matching -- a continuity signal independent of HandEngine's
    per-frame handedness guess. Prefers the tight bbox (hand_scale = its
    longer side); falls back to the wrist/middle-MCP keypoints (hand_scale
    approximated the same way normalize_hand_landmarks scales the skeleton)
    when the box message is stale or missing."""
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
        # Per (camera, hand_index) label-vote history and smoothed label --
        # see smooth_hand_label. Bounded: cameras x the few hand indexes
        # HandEngine ever assigns.
        self._hand_label_votes: Dict[Tuple[str, int], Deque[str]] = {}
        self._hand_label_current: Dict[Tuple[str, int], str] = {}
        # Per-role 3D skeleton tracks -- see HandRoleTrack / the lifecycle
        # constants above. Written from ROS callbacks, read from the asyncio
        # broadcast loop; individual attribute reads/writes are GIL-atomic,
        # same unsynchronized pattern as hand_latest_snapshot.
        self._hand_role_tracks: Dict[str, HandRoleTrack] = {}
        # Last (x, y, hand_scale, monotonic_ts) seen for each (camera, role)
        # -- the positional-continuity memory _assign_hand_roles matches new
        # detections against. Bounded: cameras x 2 roles.
        self._hand_role_anchor: Dict[Tuple[str, str], Tuple[float, float, float, float]] = {}

    def _on_hand_boxes(self, camera_name: str, msg, is_live: bool = True) -> None:
        # Live and playback are two separate subscriptions (playback's is
        # remapped to /bagplay/... -- see bagplay_topic / PlaybackManager),
        # gated exactly like the image/pose callbacks: is_live ==
        # self._playback_mode means "wrong source for the current mode".
        if is_live == self._playback_mode:
            return
        self.hand_overlay_available.add(camera_name)
        self._hand_latest_boxes[camera_name] = msg

    def _on_hand_keypoints(self, camera_name: str, msg, is_live: bool = True) -> None:
        if is_live == self._playback_mode:
            return
        self.hand_overlay_available.add(camera_name)
        # Parsed regardless of the Settings 2D-overlay toggle: the snapshot
        # also feeds the 3D hand skeleton via hand_landmarks_for_role, which
        # has no per-camera toggle. Only cameras actually running HandEngine
        # publish here, and a Detection2DArray walk (~42 entries) is cheap
        # next to the per-frame image work.
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

            # Feed the per-role 3D skeleton tracks -- see _assign_hand_roles
            # for why this is positional matching rather than "whichever hand
            # HandEngine's label says is left/right this frame".
            for entry, role in self._assign_hand_roles(camera_name, hands, time.monotonic()):
                landmarks = normalize_hand_landmarks(entry.get("keypoints", []))
                if landmarks is None:
                    continue
                self._hand_track_hit(role, landmarks)

    def _assign_hand_roles(
        self, camera_name: str, hands: List[Dict[str, object]], now: float
    ) -> List[Tuple[Dict[str, object], str]]:
        """Decide which detected hand feeds which teleop role this message.

        HandEngine's hand_index is not a persistent track: with two hands in
        view it can swap between them message to message, and MediaPipe is
        known to sometimes label both hands the same (google/mediapipe
        #3902) -- either way, trusting class_id directly made the 3D
        skeleton jump between physical hands even when each hand's own
        label was internally stable. Prefer positional continuity instead,
        in three stages: (1) sticky -- each role independently checks only
        "is my own last hand still right here" (HAND_POSITION_STICKY_FACTOR,
        no comparison against the other role), which is what prevents
        continuous back-and-forth flipping between two hands that are both
        just sitting within the old single wide threshold of each other;
        (2) wider nearest-neighbor competition (HAND_POSITION_JUMP_FACTOR)
        for a role stickiness didn't resolve; (3) smoothed classifier label
        to seed a role with no recent position at all -- track birth, or
        both hands appearing at once.
        """
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

        # Stage 1 -- sticky continuity: each role independently looks for
        # its own closest candidate within the tight radius, never
        # comparing against the other role's distance. A hand that hasn't
        # moved much wins its role outright here and is never put up
        # against the other hand's distance at all, which is what stops
        # per-frame jitter from flipping the pairing.
        sticky_pairs = []
        for role in HAND_CLASS_TO_ROLE.values():
            prev = self._hand_role_anchor.get((camera_name, role))
            if prev is None or now - prev[3] > HAND_TRACK_COAST_SEC:
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

        # Stage 2 -- wider competition, only for a role stickiness didn't
        # resolve (its hand moved further than a jitter, or briefly wasn't
        # detected). This is the plain nearest-of-remaining-candidates match
        # from before; it can still occasionally pick wrong when a role's
        # position is genuinely stale, but it no longer runs every single
        # frame against a hand that never left.
        wide_pairs = []
        for role in HAND_CLASS_TO_ROLE.values():
            if role in role_taken:
                continue
            prev = self._hand_role_anchor.get((camera_name, role))
            if prev is None or now - prev[3] > HAND_TRACK_COAST_SEC:
                continue
            px, py, pscale, _ = prev
            for idx, (_, (x, y, scale)) in enumerate(candidates):
                if idx in idx_taken:
                    continue
                dist = math.hypot(x - px, y - py)
                if dist <= HAND_POSITION_JUMP_FACTOR * max(pscale, scale):
                    wide_pairs.append((dist, role, idx))
        _apply(wide_pairs)

        # Stage 3 -- classifier fallback for a hand with no continuity at
        # all (track birth, or both hands appearing at once).
        for idx, (entry, _) in enumerate(candidates):
            if idx in idx_taken:
                continue
            role = HAND_CLASS_TO_ROLE.get(str(entry.get("label", "")))
            if role is None or role in role_taken:
                continue
            role_taken.add(role)
            idx_taken.add(idx)
            assignments.append((entry, role))

        for entry, role in assignments:
            anchor = next(a for e, a in candidates if e is entry)
            self._hand_role_anchor[(camera_name, role)] = (anchor[0], anchor[1], anchor[2], now)
        return assignments

    def _hand_track_hit(
        self, role: str, landmarks: List[Optional[List[float]]]
    ) -> None:
        """Record one confident detection of `role`; births the track once
        HAND_TRACK_BIRTH_MIN_HITS consecutive hits accumulate."""
        now = time.monotonic()
        track = self._hand_role_tracks.setdefault(role, HandRoleTrack())
        if now - track.last_hit_monotonic > HAND_TRACK_BIRTH_MAX_GAP_SEC:
            # Too long since the previous hit: an unborn streak restarts. A
            # visible track is unaffected -- its death is coast-timeout only
            # (in hand_landmarks_for_role), and any hit refreshes it.
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
        else:
            self.hand_overlay_enabled.discard(camera_name)

    def hand_landmarks_for_role(self, role: str) -> Optional[List[Optional[List[float]]]]:
        """Normalized 21-point landmarks for a teleop role ("left_hand" /
        "right_hand") from that role's lifecycle track (fed by every
        HandEngine camera -- any of them may see either hand). During a
        detection dropout the last-seen shape is held for up to
        HAND_TRACK_COAST_SEC, so per-frame recall misses don't blink the 3D
        skeleton. Returns None while the track is unborn or dead."""
        track = self._hand_role_tracks.get(role)
        if track is None or not track.visible:
            return None
        if time.monotonic() - track.last_hit_monotonic > HAND_TRACK_COAST_SEC:
            track.visible = False
            track.streak = 0
            track.landmarks = None
            return None
        return track.landmarks

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
