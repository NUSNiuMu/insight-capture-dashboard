"""Recognize the double-thumbs-up gesture and debounce its trigger."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


THUMB_POINTS = (0, 2, 3, 4, 5, 9, 13, 17)
FINGER_CHAINS = (
    (5, 6, 7, 8),
    (9, 10, 11, 12),
    (13, 14, 15, 16),
    (17, 18, 19, 20),
)


def _distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angle(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    """Return the smaller angle ABC in degrees."""
    ab = (a[0] - b[0], a[1] - b[1])
    cb = (c[0] - b[0], c[1] - b[1])
    denom = math.hypot(*ab) * math.hypot(*cb)
    if denom < 1e-6:
        return 0.0
    cosine = max(-1.0, min(1.0, (ab[0] * cb[0] + ab[1] * cb[1]) / denom))
    return math.degrees(math.acos(cosine))


def _points(
    hand: Dict[str, object], min_score: float
) -> Dict[int, Tuple[float, float]]:
    return {
        int(kp["i"]): (float(kp["x"]), float(kp["y"]))
        for kp in hand.get("keypoints", [])
        if float(kp.get("score", 0.0)) >= min_score
    }


@dataclass(frozen=True)
class HandThumbsUpResult:
    matched: bool
    reason: str
    curled_fingers: int = 0


@dataclass(frozen=True)
class DoubleThumbsUpResult:
    active: bool
    hands_detected: int
    matching_hands: int
    reasons: Tuple[str, ...]


def classify_thumbs_up(
    hand: Dict[str, object], min_score: float = 0.35
) -> HandThumbsUpResult:
    """Match an upward extended thumb with all four other fingers curled."""
    points = _points(hand, min_score)
    required = set(THUMB_POINTS)
    for chain in FINGER_CHAINS:
        required.update(chain)
    if not required.issubset(points):
        return HandThumbsUpResult(False, "missing_keypoints")

    wrist = points[0]
    palm_scale = _distance(wrist, points[9])
    if palm_scale < 8.0:
        return HandThumbsUpResult(False, "hand_too_small")

    thumb_mcp, thumb_ip, thumb_tip = points[2], points[3], points[4]
    thumb_rise = thumb_mcp[1] - thumb_tip[1]
    thumb_dx = abs(thumb_tip[0] - thumb_mcp[0])
    thumb_straight = _angle(thumb_mcp, thumb_ip, thumb_tip) >= 145.0
    thumb_long = _distance(thumb_mcp, thumb_tip) >= 0.70 * palm_scale
    thumb_vertical = thumb_rise >= 0.75 * palm_scale and thumb_dx <= 0.85 * thumb_rise
    above_knuckles = thumb_tip[1] <= min(points[index][1] for index in (5, 9, 13, 17)) - 0.15 * palm_scale
    if not (thumb_straight and thumb_long and thumb_vertical and above_knuckles):
        return HandThumbsUpResult(False, "thumb_not_up")

    curled = 0
    for mcp_index, pip_index, dip_index, tip_index in FINGER_CHAINS:
        mcp = points[mcp_index]
        pip = points[pip_index]
        dip = points[dip_index]
        tip = points[tip_index]
        pip_bent = _angle(mcp, pip, dip) <= 150.0
        tip_folded = _distance(tip, wrist) <= 1.25 * _distance(pip, wrist)
        tip_not_raised = tip[1] >= mcp[1] - 0.35 * palm_scale
        if pip_bent and tip_folded and tip_not_raised:
            curled += 1
    if curled != 4:
        return HandThumbsUpResult(False, "fingers_not_curled", curled)
    return HandThumbsUpResult(True, "matched", curled)


def classify_double_thumbs_up(
    hands: List[Dict[str, object]], min_score: float = 0.35
) -> DoubleThumbsUpResult:
    """Require exactly two detected hands and a thumbs-up match on both."""
    if len(hands) != 2:
        return DoubleThumbsUpResult(False, len(hands), 0, ("requires_two_hands",))
    results = tuple(classify_thumbs_up(hand, min_score) for hand in hands)
    matching = sum(1 for result in results if result.matched)
    return DoubleThumbsUpResult(
        active=matching == 2,
        hands_detected=2,
        matching_hands=matching,
        reasons=tuple(result.reason for result in results),
    )


@dataclass(frozen=True)
class GestureLatchSnapshot:
    phase: str
    active: bool
    hold_progress: float
    release_progress: float


class DoubleThumbsUpLatch:
    """Emit once after a hold, then require a continuous release to re-arm."""

    def __init__(
        self,
        hold_sec: float = 0.8,
        release_sec: float = 2.0,
        hold_gap_sec: float = 0.15,
    ) -> None:
        self.hold_sec = max(float(hold_sec), 0.05)
        self.release_sec = max(float(release_sec), 0.05)
        self.hold_gap_sec = max(float(hold_gap_sec), 0.0)
        self.armed = True
        self._hold_since: Optional[float] = None
        self._last_active: Optional[float] = None
        self._release_since: Optional[float] = None
        self._active = False

    def update(self, active: bool, now: float) -> bool:
        self._active = bool(active)
        if self.armed:
            self._release_since = None
            if not active:
                if (
                    self._hold_since is not None
                    and self._last_active is not None
                    and now - self._last_active <= self.hold_gap_sec
                ):
                    return False
                self._hold_since = None
                self._last_active = None
                return False
            self._last_active = now
            if self._hold_since is None:
                self._hold_since = now
                return False
            if now - self._hold_since < self.hold_sec:
                return False
            self.armed = False
            self._hold_since = None
            self._last_active = None
            return True

        self._hold_since = None
        self._last_active = None
        if active:
            self._release_since = None
            return False
        if self._release_since is None:
            self._release_since = now
            return False
        if now - self._release_since >= self.release_sec:
            self.armed = True
            self._release_since = None
        return False

    def snapshot(self, now: float) -> GestureLatchSnapshot:
        if self.armed and self._hold_since is not None:
            phase = "holding"
        elif self.armed:
            phase = "armed"
        else:
            phase = "release_required"
        hold_elapsed = 0.0 if self._hold_since is None else max(0.0, now - self._hold_since)
        release_elapsed = 0.0 if self._release_since is None else max(0.0, now - self._release_since)
        return GestureLatchSnapshot(
            phase=phase,
            active=self._active,
            hold_progress=min(1.0, hold_elapsed / self.hold_sec),
            release_progress=min(1.0, release_elapsed / self.release_sec),
        )
