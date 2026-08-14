"""Fixed-station three-camera relative-pose quality gate."""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, Optional, Tuple

from .models import PoseSample


REQUIRED_ROLES = ("head", "left_hand", "right_hand")
ROLE_PAIRS = (
    ("head", "left_hand"),
    ("head", "right_hand"),
    ("left_hand", "right_hand"),
)


def _normalize_quaternion(values: Iterable[float]) -> Tuple[float, float, float, float]:
    quaternion = tuple(float(value) for value in values)
    norm = math.sqrt(sum(value * value for value in quaternion))
    if norm <= 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(value / norm for value in quaternion)  # type: ignore[return-value]


def _quaternion_conjugate(
    quaternion: Tuple[float, float, float, float]
) -> Tuple[float, float, float, float]:
    x, y, z, w = quaternion
    return (-x, -y, -z, w)


def _quaternion_multiply(
    left: Tuple[float, float, float, float],
    right: Tuple[float, float, float, float],
) -> Tuple[float, float, float, float]:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    return _normalize_quaternion(
        (
            lw * rx + lx * rw + ly * rz - lz * ry,
            lw * ry - lx * rz + ly * rw + lz * rx,
            lw * rz + lx * ry - ly * rx + lz * rw,
            lw * rw - lx * rx - ly * ry - lz * rz,
        )
    )


def _rotate_vector(
    quaternion: Tuple[float, float, float, float],
    vector: Tuple[float, float, float],
) -> Tuple[float, float, float]:
    x, y, z, w = quaternion
    vx, vy, vz = vector
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def _rotation_error_deg(
    left: Tuple[float, float, float, float],
    right: Tuple[float, float, float, float],
) -> float:
    dot = abs(sum(a * b for a, b in zip(left, right)))
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, dot))))


def _distance(left: Iterable[float], right: Iterable[float]) -> float:
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left, right)))


def _average_quaternion(
    quaternions: Iterable[Tuple[float, float, float, float]]
) -> Tuple[float, float, float, float]:
    values = list(quaternions)
    anchor = values[0]
    aligned = [
        tuple(-value for value in item)
        if sum(a * b for a, b in zip(anchor, item)) < 0.0
        else item
        for item in values
    ]
    return _normalize_quaternion(
        tuple(sum(item[index] for item in aligned) for index in range(4))
    )


def _relative_pose(source: dict, target: dict) -> dict:
    source_position = tuple(source["position"])
    target_position = tuple(target["position"])
    source_quaternion = _normalize_quaternion(source["quaternion_xyzw"])
    target_quaternion = _normalize_quaternion(target["quaternion_xyzw"])
    inverse = _quaternion_conjugate(source_quaternion)
    delta = tuple(b - a for a, b in zip(source_position, target_position))
    return {
        "translation_m": [round(value, 6) for value in _rotate_vector(inverse, delta)],
        "quaternion_xyzw": [
            round(value, 7)
            for value in _quaternion_multiply(inverse, target_quaternion)
        ],
    }


class CaptureCheckManager:
    """Measure a stable station pose and compare its pairwise camera geometry."""

    def __init__(
        self,
        *,
        pose_roles: Dict[str, str],
        mapping_snapshot: Callable[[], Dict[str, object]],
        results_root: Path,
        config: Optional[Dict[str, object]] = None,
    ) -> None:
        settings = dict(config or {})
        self.enabled = bool(settings.get("enabled", True))
        self.sample_window_sec = max(0.5, float(settings.get("sample_window_sec", 1.2)))
        self.minimum_window_sec = max(
            0.2, min(self.sample_window_sec, float(settings.get("minimum_window_sec", 0.8)))
        )
        self.minimum_samples = max(3, int(settings.get("minimum_samples", 12)))
        self.maximum_pose_age_sec = max(
            0.1, float(settings.get("maximum_pose_age_sec", 0.5))
        )
        self.stationary_translation_m = max(
            0.001, float(settings.get("stationary_translation_m", 0.01))
        )
        self.stationary_rotation_deg = max(
            0.1, float(settings.get("stationary_rotation_deg", 2.0))
        )
        self.pass_translation_m = max(
            0.001, float(settings.get("pass_translation_m", 0.015))
        )
        self.pass_rotation_deg = max(
            0.1, float(settings.get("pass_rotation_deg", 3.0))
        )
        self.recalibrate_translation_m = max(
            self.pass_translation_m,
            float(settings.get("recalibrate_translation_m", 0.03)),
        )
        self.recalibrate_rotation_deg = max(
            self.pass_rotation_deg,
            float(settings.get("recalibrate_rotation_deg", 6.0)),
        )
        self.minimum_map_points = max(0, int(settings.get("minimum_map_points", 30)))
        self.pose_roles = dict(pose_roles)
        self.role_names = {
            role: name for name, role in self.pose_roles.items() if role in REQUIRED_ROLES
        }
        self.mapping_snapshot = mapping_snapshot
        self._lock = threading.Lock()
        max_samples = max(120, int(self.sample_window_sec * 120.0))
        self._samples: Dict[str, Deque[tuple[float, PoseSample]]] = {
            name: deque(maxlen=max_samples) for name in self.pose_roles
        }
        self.output_dir = Path(results_root).resolve() / "capture_checks"
        self.reference_path = self.output_dir / "reference.json"
        self.history_path = self.output_dir / "history.jsonl"
        self.reference = self._load_reference()
        self.last_result: Optional[dict] = None

    def record_pose(
        self, name: str, sample: PoseSample, received_monotonic: Optional[float] = None
    ) -> None:
        if not self.enabled or name not in self._samples:
            return
        received = time.monotonic() if received_monotonic is None else received_monotonic
        with self._lock:
            self._samples[name].append((received, sample))

    def snapshot(self, *, bag_name: Optional[str] = None) -> dict:
        measurement, reasons = self._measure()
        state = "ready" if measurement is not None else "not_ready"
        if not self.enabled:
            state = "disabled"
        elif self.reference is None and measurement is not None:
            state = "no_reference"
        return {
            "type": "capture_check",
            "state": state,
            "enabled": self.enabled,
            "reference_available": self.reference is not None,
            "reference_created_at": (
                self.reference.get("created_at") if self.reference else None
            ),
            "bag_name": bag_name,
            "readiness": {
                "ready": measurement is not None,
                "reasons": reasons,
                "measurement": measurement,
            },
            "thresholds": self._threshold_payload(),
            "last_result": self.last_result,
        }

    def set_reference(self) -> dict:
        measurement, reasons = self._measure()
        if measurement is None:
            result = self._result("not_ready", reasons=reasons)
            self.last_result = result
            return result
        reference = {
            "version": 1,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "role_names": dict(self.role_names),
            "pairs": measurement["pairs"],
            "thresholds": self._threshold_payload(),
        }
        self.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.reference_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(reference, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.reference_path)
        self.reference = reference
        result = self._result("reference_saved", measurement=measurement)
        self.last_result = result
        self._append_history(result)
        return result

    def check(self, *, bag_name: Optional[str] = None) -> dict:
        measurement, reasons = self._measure()
        if measurement is None:
            result = self._result("not_ready", bag_name=bag_name, reasons=reasons)
        elif self.reference is None:
            result = self._result("no_reference", bag_name=bag_name)
        elif self.reference.get("role_names") != self.role_names:
            result = self._result(
                "no_reference",
                bag_name=bag_name,
                reasons=["camera configuration changed; capture a new station reference"],
            )
        else:
            comparisons = {}
            worst_state = "pass"
            for pair_name, current in measurement["pairs"].items():
                baseline = self.reference["pairs"].get(pair_name)
                if not isinstance(baseline, dict):
                    result = self._result(
                        "no_reference",
                        bag_name=bag_name,
                        reasons=[f"reference is missing pair {pair_name}"],
                    )
                    break
                translation_error = _distance(
                    current["translation_m"], baseline["translation_m"]
                )
                rotation_error = _rotation_error_deg(
                    _normalize_quaternion(current["quaternion_xyzw"]),
                    _normalize_quaternion(baseline["quaternion_xyzw"]),
                )
                pair_state = self._comparison_state(translation_error, rotation_error)
                if pair_state == "recalibrate":
                    worst_state = "recalibrate"
                elif pair_state == "retry" and worst_state == "pass":
                    worst_state = "retry"
                comparisons[pair_name] = {
                    "state": pair_state,
                    "translation_error_m": round(translation_error, 5),
                    "rotation_error_deg": round(rotation_error, 3),
                }
            else:
                suspects = self._suspect_roles(comparisons)
                result = self._result(
                    worst_state,
                    bag_name=bag_name,
                    measurement=measurement,
                    comparisons=comparisons,
                    suspect_roles=suspects,
                    suspect_cameras=[self.role_names[role] for role in suspects],
                )
        self.last_result = result
        self._append_history(result)
        if bag_name:
            self._write_bag_result(bag_name, result)
        return result

    def _measure(self) -> tuple[Optional[dict], list[str]]:
        if not self.enabled:
            return None, ["capture check is disabled for this device profile"]
        missing_roles = [role for role in REQUIRED_ROLES if role not in self.role_names]
        if missing_roles:
            return None, [f"missing pose roles: {', '.join(missing_roles)}"]
        mapping = self.mapping_snapshot()
        statuses = mapping.get("statuses") if isinstance(mapping, dict) else None
        statuses = statuses if isinstance(statuses, dict) else {}
        reasons = []
        mapper = statuses.get("insight9")
        if not isinstance(mapper, dict) or not mapper.get("online"):
            reasons.append("Insight9 mapping status is offline")
        if int(mapping.get("map_point_count", 0) or 0) < self.minimum_map_points:
            reasons.append(f"map has fewer than {self.minimum_map_points} confirmed points")
        for status_name in ("insight3_a", "insight3_b"):
            status = statuses.get(status_name)
            if not isinstance(status, dict) or not status.get("online"):
                reasons.append(f"{status_name} localization status is offline")
            elif not status.get("localized"):
                reasons.append(f"{status_name} is not globally localized")
            elif status.get("tracking_mode") != "map_matched":
                reasons.append(f"{status_name} is not currently matched to the map")

        now = time.monotonic()
        poses = {}
        with self._lock:
            sample_sets = {
                role: [
                    (stamp, sample)
                    for stamp, sample in self._samples[self.role_names[role]]
                    if now - stamp <= self.sample_window_sec
                ]
                for role in REQUIRED_ROLES
            }
        for role, samples in sample_sets.items():
            name = self.role_names[role]
            if len(samples) < self.minimum_samples:
                reasons.append(f"{name} has only {len(samples)} recent pose samples")
                continue
            if now - samples[-1][0] > self.maximum_pose_age_sec:
                reasons.append(f"{name} pose is stale")
                continue
            if samples[-1][0] - samples[0][0] < self.minimum_window_sec:
                reasons.append(f"{name} stable window is too short")
                continue
            positions = [sample.position for _, sample in samples]
            quaternions = [
                _normalize_quaternion(sample.orientation_xyzw) for _, sample in samples
            ]
            position = tuple(
                statistics.median(item[index] for item in positions) for index in range(3)
            )
            quaternion = _average_quaternion(quaternions)
            translation_spread = max(_distance(item, position) for item in positions)
            rotation_spread = max(
                _rotation_error_deg(item, quaternion) for item in quaternions
            )
            if translation_spread > self.stationary_translation_m:
                reasons.append(
                    f"{name} is moving ({translation_spread * 1000.0:.1f} mm spread)"
                )
            if rotation_spread > self.stationary_rotation_deg:
                reasons.append(f"{name} is rotating ({rotation_spread:.1f} deg spread)")
            poses[role] = {
                "camera": name,
                "position": [round(value, 6) for value in position],
                "quaternion_xyzw": [round(value, 7) for value in quaternion],
                "samples": len(samples),
                "window_sec": round(samples[-1][0] - samples[0][0], 3),
                "translation_spread_m": round(translation_spread, 5),
                "rotation_spread_deg": round(rotation_spread, 3),
            }
        if reasons:
            return None, reasons
        pairs = {
            f"{source}->{target}": _relative_pose(poses[source], poses[target])
            for source, target in ROLE_PAIRS
        }
        return {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "poses": poses,
            "pairs": pairs,
        }, []

    def _comparison_state(self, translation_error: float, rotation_error: float) -> str:
        if (
            translation_error > self.recalibrate_translation_m
            or rotation_error > self.recalibrate_rotation_deg
        ):
            return "recalibrate"
        if (
            translation_error > self.pass_translation_m
            or rotation_error > self.pass_rotation_deg
        ):
            return "retry"
        return "pass"

    @staticmethod
    def _suspect_roles(comparisons: dict) -> list[str]:
        failing = [name for name, item in comparisons.items() if item["state"] != "pass"]
        counts = {role: 0 for role in REQUIRED_ROLES}
        for pair_name in failing:
            source, target = pair_name.split("->", 1)
            counts[source] += 1
            counts[target] += 1
        suspects = [role for role, count in counts.items() if count >= 2]
        return suspects if len(suspects) == 1 else []

    def _threshold_payload(self) -> dict:
        return {
            "stationary_translation_m": self.stationary_translation_m,
            "stationary_rotation_deg": self.stationary_rotation_deg,
            "pass_translation_m": self.pass_translation_m,
            "pass_rotation_deg": self.pass_rotation_deg,
            "recalibrate_translation_m": self.recalibrate_translation_m,
            "recalibrate_rotation_deg": self.recalibrate_rotation_deg,
            "minimum_map_points": self.minimum_map_points,
        }

    @staticmethod
    def _result(state: str, **payload: object) -> dict:
        return {
            "type": "capture_check_result",
            "state": state,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **payload,
        }

    def _load_reference(self) -> Optional[dict]:
        try:
            payload = json.loads(self.reference_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) and payload.get("version") == 1 else None

    def _append_history(self, result: dict) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as history:
            history.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    def _write_bag_result(self, bag_name: str, result: dict) -> None:
        safe_name = Path(str(bag_name)).name
        if safe_name != bag_name or safe_name in ("", ".", ".."):
            return
        path = self.output_dir / f"{safe_name}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
