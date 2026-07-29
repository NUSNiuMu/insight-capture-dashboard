"""Track, gate, and stabilize offline hand-pose frames."""

from __future__ import annotations

from collections import deque
import math
from typing import List


PLAUSIBLE_BOUNDS = {"x": (-0.6, 0.6), "y": (-0.6, 0.9), "z": (0.1, 1.3)}


def _wrist(hand: dict) -> list:
    return hand["p"][0:3]


def _distance(a: list, b: list) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _in_bounds(hand: dict) -> bool:
    x, y, z = _wrist(hand)
    bx, by, bz = (
        PLAUSIBLE_BOUNDS["x"],
        PLAUSIBLE_BOUNDS["y"],
        PLAUSIBLE_BOUNDS["z"],
    )
    return bx[0] <= x <= bx[1] and by[0] <= y <= by[1] and bz[0] <= z <= bz[1]


class _LowPass:
    def __init__(self) -> None:
        self.value = None

    def filter(self, value: float, alpha: float) -> float:
        self.value = (
            value
            if self.value is None
            else alpha * value + (1.0 - alpha) * self.value
        )
        return self.value


class _OneEuroFilter:
    def __init__(
        self,
        frequency: float = 30.0,
        min_cutoff: float = 1.5,
        beta: float = 0.3,
        derivative_cutoff: float = 1.0,
    ) -> None:
        self.frequency = frequency
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.derivative_cutoff = derivative_cutoff
        self.value_filter = _LowPass()
        self.derivative_filter = _LowPass()
        self.last_time = None

    def _alpha(self, cutoff: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        sample_period = 1.0 / self.frequency
        return 1.0 / (1.0 + tau / sample_period)

    def filter(self, value: float, timestamp: float) -> float:
        if self.last_time is not None and timestamp > self.last_time:
            self.frequency = 1.0 / (timestamp - self.last_time)
        self.last_time = timestamp
        previous = self.value_filter.value
        derivative = (
            0.0 if previous is None else (value - previous) * self.frequency
        )
        filtered_derivative = self.derivative_filter.filter(
            derivative, self._alpha(self.derivative_cutoff)
        )
        cutoff = self.min_cutoff + self.beta * abs(filtered_derivative)
        return self.value_filter.filter(value, self._alpha(cutoff))


def _dominant_label(votes: deque) -> str:
    """Return the majority label, preferring the most recent vote on ties."""
    counts = {label: votes.count(label) for label in set(votes)}
    best_count = max(counts.values())
    return next(
        label for label in reversed(votes) if counts[label] == best_count
    )


def stabilize_mediapipe(
    frames: List[dict],
    *,
    max_speed: float = 3.0,
    min_gate: float = 0.04,
    max_gate: float = 0.3,
    track_timeout_ms: int = 750,
    confirmation_frames: int = 2,
    min_cutoff: float = 1.5,
    beta: float = 0.3,
) -> List[dict]:
    """Track in image space and filter hand-relative MediaPipe landmarks."""
    tracks = []
    next_id = 0
    output = [{"t": int(record["t"]), "h": []} for record in frames]
    confirmation_frames = max(1, int(confirmation_frames))

    for frame_index, record in enumerate(frames):
        timestamp_ms = int(record["t"])
        timestamp_sec = timestamp_ms / 1000.0
        hands = [
            hand
            for hand in record.get("h", [])
            if len(hand.get("a", [])) == 2 and len(hand.get("p", [])) == 63
        ]
        tracks[:] = [
            track
            for track in tracks
            if timestamp_ms - track["t"] <= track_timeout_ms
        ]

        candidates = []
        for hand_index, hand in enumerate(hands):
            for track_index, track in enumerate(tracks):
                delta_sec = max(1, timestamp_ms - track["t"]) / 1000.0
                gate = min(max_gate, max(min_gate, max_speed * delta_sec))
                distance = _distance(hand["a"], track["anchor"])
                if distance <= gate:
                    label_penalty = (
                        0.04
                        if hand["c"] != _dominant_label(track["labels"])
                        else 0.0
                    )
                    candidates.append(
                        (distance + label_penalty, hand_index, track_index)
                    )
        candidates.sort()

        assignments = {}
        used_hands = set()
        used_tracks = set()
        for _cost, hand_index, track_index in candidates:
            if hand_index in used_hands or track_index in used_tracks:
                continue
            assignments[hand_index] = track_index
            used_hands.add(hand_index)
            used_tracks.add(track_index)

        for track_index, track in enumerate(tracks):
            if track_index not in used_tracks:
                track["streak"] = 0
                track["pending"] = []

        for hand_index, hand in enumerate(hands):
            if hand_index in assignments:
                track = tracks[assignments[hand_index]]
                if track["last_frame"] != frame_index - 1:
                    track["streak"] = 0
                    track["pending"] = []
                track["anchor"] = list(hand["a"])
                track["t"] = timestamp_ms
                track["last_frame"] = frame_index
                track["streak"] += 1
                track["labels"].append(hand["c"])
                points = [
                    item_filter.filter(value, timestamp_sec)
                    for item_filter, value in zip(track["filters"], hand["p"])
                ]
                filtered = {
                    "c": _dominant_label(track["labels"]),
                    "s": hand["s"],
                    "p": points,
                }
                if track["streak"] == confirmation_frames:
                    for pending_index, pending_hand in track["pending"]:
                        pending_hand["c"] = filtered["c"]
                        output[pending_index]["h"].append(
                            (track["id"], pending_hand)
                        )
                    track["pending"] = []
                if track["streak"] >= confirmation_frames:
                    output[frame_index]["h"].append((track["id"], filtered))
                else:
                    track["pending"].append((frame_index, filtered))
                continue

            filters = [
                _OneEuroFilter(min_cutoff=min_cutoff, beta=beta)
                for _ in range(63)
            ]
            points = [
                item_filter.filter(value, timestamp_sec)
                for item_filter, value in zip(filters, hand["p"])
            ]
            labels = deque([hand["c"]], maxlen=5)
            filtered = {
                "c": hand["c"],
                "s": hand["s"],
                "p": points,
            }
            pending = (
                []
                if confirmation_frames <= 1
                else [(frame_index, filtered)]
            )
            track = {
                "id": next_id,
                "anchor": list(hand["a"]),
                "t": timestamp_ms,
                "last_frame": frame_index,
                "streak": 1,
                "pending": pending,
                "filters": filters,
                "labels": labels,
            }
            tracks.append(track)
            if confirmation_frames <= 1:
                output[frame_index]["h"].append((next_id, filtered))
            next_id += 1

    cleaned = []
    for record in output:
        if not record["h"]:
            continue
        record["h"].sort(key=lambda item: item[0])
        hands = [
            {
                "c": hand["c"],
                "s": hand["s"],
                "p": [round(value, 4) for value in hand["p"]],
            }
            for _track_id, hand in record["h"]
        ]
        cleaned.append({"t": record["t"], "h": hands})
    return cleaned


def stabilize_wilor(
    frames: List[dict],
    *,
    max_speed: float = 4.0,
    min_gate: float = 0.05,
    max_gate: float = 0.35,
    track_timeout_ms: int = 2000,
    min_cutoff: float = 1.5,
    beta: float = 0.3,
) -> List[dict]:
    """Return confirmed, physically plausible, One-Euro-filtered tracks."""
    tracks = []
    next_id = 0
    output = [{"t": record["t"], "h": []} for record in frames]

    for frame_index, record in enumerate(frames):
        timestamp_ms = int(record["t"])
        hands = [hand for hand in record.get("h", []) if _in_bounds(hand)]
        candidates = []
        for hand_index, hand in enumerate(hands):
            for track_index, track in enumerate(tracks):
                delta_sec = max(1, timestamp_ms - track["t"]) / 1000.0
                gate = min(max_gate, max(min_gate, max_speed * delta_sec))
                distance = _distance(_wrist(hand), track["position"])
                if distance <= gate:
                    candidates.append((distance, hand_index, track_index))
        candidates.sort()

        assignments = {}
        used_hands = set()
        used_tracks = set()
        for _distance_value, hand_index, track_index in candidates:
            if hand_index in used_hands or track_index in used_tracks:
                continue
            assignments[hand_index] = track_index
            used_hands.add(hand_index)
            used_tracks.add(track_index)

        for hand_index, hand in enumerate(hands):
            timestamp_sec = timestamp_ms / 1000.0
            if hand_index in assignments:
                track = tracks[assignments[hand_index]]
                track["position"] = _wrist(hand)
                track["t"] = timestamp_ms
                track["streak"] += 1
                points = [
                    item_filter.filter(value, timestamp_sec)
                    for item_filter, value in zip(track["filters"], hand["p"])
                ]
                filtered = {"c": hand["c"], "s": hand["s"], "p": points}
                if track["streak"] == 2:
                    for pending_index, pending_hand in track["pending"]:
                        output[pending_index]["h"].append(
                            (track["id"], pending_hand)
                        )
                    track["pending"] = []
                if track["streak"] >= 2:
                    output[frame_index]["h"].append((track["id"], filtered))
                else:
                    track["pending"].append((frame_index, filtered))
                continue

            filters = [
                _OneEuroFilter(min_cutoff=min_cutoff, beta=beta)
                for _ in range(63)
            ]
            points = [
                item_filter.filter(value, timestamp_sec)
                for item_filter, value in zip(filters, hand["p"])
            ]
            tracks.append(
                {
                    "id": next_id,
                    "position": _wrist(hand),
                    "t": timestamp_ms,
                    "streak": 1,
                    "pending": [
                        (
                            frame_index,
                            {"c": hand["c"], "s": hand["s"], "p": points},
                        )
                    ],
                    "filters": filters,
                }
            )
            next_id += 1

        tracks[:] = [
            track
            for track in tracks
            if timestamp_ms - track["t"] < track_timeout_ms
        ]

    cleaned = []
    for record in output:
        if not record["h"]:
            continue
        record["h"].sort(key=lambda item: item[0])
        hands = [
            {
                "c": hand["c"],
                "s": hand["s"],
                "p": [round(value, 4) for value in hand["p"]],
            }
            for _track_id, hand in record["h"]
        ]
        cleaned.append({"t": record["t"], "h": hands})
    return cleaned
