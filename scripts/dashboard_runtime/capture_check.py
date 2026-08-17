"""Episode gate using hand-camera stations and Insight9 natural-map closure."""

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
FIXED_STATION_ROLES = ("left_hand", "right_hand")


def _normalize_quaternion(values: Iterable[float]) -> Tuple[float, float, float, float]:
    quaternion = tuple(float(value) for value in values)
    norm = math.sqrt(sum(value * value for value in quaternion))
    if norm <= 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(value / norm for value in quaternion)  # type: ignore[return-value]


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


class CaptureCheckManager:
    """Validate docked hand cameras and fresh Insight9 closure to a frozen map."""

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
        self.maximum_insight9_validation_age_sec = max(
            0.5,
            float(settings.get("maximum_insight9_validation_age_sec", 5.0)),
        )
        self.thresholds = {
            "insight3": self._parse_thresholds(
                settings.get("insight3"),
                stationary_translation_m=0.006,
                stationary_rotation_deg=1.5,
                pass_translation_m=0.01,
                pass_rotation_deg=2.0,
                recalibrate_translation_m=0.025,
                recalibrate_rotation_deg=5.0,
            ),
            "insight9": self._parse_thresholds(
                settings.get("insight9_closure", settings.get("insight9")),
                stationary_translation_m=0.025,
                stationary_rotation_deg=5.0,
                pass_translation_m=0.04,
                pass_rotation_deg=8.0,
                recalibrate_translation_m=0.08,
                recalibrate_rotation_deg=15.0,
            ),
        }
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

    @staticmethod
    def _parse_thresholds(
        payload: object,
        *,
        stationary_translation_m: float,
        stationary_rotation_deg: float,
        pass_translation_m: float,
        pass_rotation_deg: float,
        recalibrate_translation_m: float,
        recalibrate_rotation_deg: float,
    ) -> dict:
        settings = payload if isinstance(payload, dict) else {}
        parsed = {
            "stationary_translation_m": max(
                0.001,
                float(
                    settings.get(
                        "stationary_translation_m", stationary_translation_m
                    )
                ),
            ),
            "stationary_rotation_deg": max(
                0.1,
                float(settings.get("stationary_rotation_deg", stationary_rotation_deg)),
            ),
            "pass_translation_m": max(
                0.001, float(settings.get("pass_translation_m", pass_translation_m))
            ),
            "pass_rotation_deg": max(
                0.1, float(settings.get("pass_rotation_deg", pass_rotation_deg))
            ),
        }
        parsed["recalibrate_translation_m"] = max(
            parsed["pass_translation_m"],
            float(
                settings.get(
                    "recalibrate_translation_m", recalibrate_translation_m
                )
            ),
        )
        parsed["recalibrate_rotation_deg"] = max(
            parsed["pass_rotation_deg"],
            float(settings.get("recalibrate_rotation_deg", recalibrate_rotation_deg)),
        )
        return parsed

    def _camera_thresholds(self, camera_name: str) -> dict:
        group = "insight3" if camera_name.startswith("insight3") else "insight9"
        return self.thresholds[group]

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

    def set_reference(self, *, insight9_reference: Optional[dict] = None) -> dict:
        measurement, reasons = self._measure()
        if measurement is None:
            result = self._result("not_ready", reasons=reasons)
            self.last_result = result
            return result
        validation = self._parse_validation_reference(insight9_reference)
        if validation is None:
            result = self._result(
                "not_ready",
                reasons=["Insight9 natural-map reference was not frozen"],
            )
            self.last_result = result
            return result
        reference = {
            "version": 3,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "role_names": dict(self.role_names),
            "poses": measurement["poses"],
            "insight9_validation": validation,
            "thresholds": self._threshold_payload(),
        }
        self._write_reference(reference)
        self.reference = reference
        result = self._result(
            "reference_saved",
            measurement=measurement,
            insight9_reference=validation,
        )
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
            suspect_roles = []
            suspect_cameras = []
            validation, validation_reason = self._current_validation(measurement)
            if validation is None:
                raw_validation = measurement.get("insight9_validation")
                reference_lost = (
                    isinstance(raw_validation, dict)
                    and not raw_validation.get("reference_active")
                )
                result = self._result(
                    "recalibrate" if reference_lost else "not_ready",
                    bag_name=bag_name,
                    reasons=[validation_reason],
                    suspect_roles=["head"] if reference_lost else [],
                    suspect_cameras=(
                        [self.role_names["head"]] if reference_lost else []
                    ),
                )
                self.last_result = result
                self._append_history(result)
                if bag_name:
                    self._write_bag_result(bag_name, result)
                return result
            reference_validation = self.reference.get("insight9_validation")
            identity_reason = self._validation_identity_reason(
                reference_validation, validation
            )
            if identity_reason:
                result = self._result(
                    "recalibrate",
                    bag_name=bag_name,
                    reasons=[identity_reason],
                    suspect_roles=["head"],
                    suspect_cameras=[self.role_names["head"]],
                )
                self.last_result = result
                self._append_history(result)
                if bag_name:
                    self._write_bag_result(bag_name, result)
                return result
            for role, current in measurement["poses"].items():
                baseline = self.reference["poses"].get(role)
                if not isinstance(baseline, dict):
                    result = self._result(
                        "no_reference",
                        bag_name=bag_name,
                        reasons=[f"reference is missing global pose for {role}"],
                    )
                    break
                translation_error = _distance(
                    current["position"], baseline["position"]
                )
                rotation_error = _rotation_error_deg(
                    _normalize_quaternion(current["quaternion_xyzw"]),
                    _normalize_quaternion(baseline["quaternion_xyzw"]),
                )
                camera_name = current["camera"]
                thresholds = self._camera_thresholds(camera_name)
                camera_state = self._comparison_state(
                    translation_error, rotation_error, thresholds
                )
                if camera_state == "recalibrate":
                    worst_state = "recalibrate"
                elif camera_state == "retry" and worst_state == "pass":
                    worst_state = "retry"
                if camera_state != "pass":
                    suspect_roles.append(role)
                    suspect_cameras.append(camera_name)
                comparisons[camera_name] = {
                    "role": role,
                    "method": "fixed_station_pose",
                    "state": camera_state,
                    "translation_error_m": round(translation_error, 5),
                    "rotation_error_deg": round(rotation_error, 3),
                    "threshold_group": (
                        "insight3" if camera_name.startswith("insight3") else "insight9"
                    ),
                }
            else:
                head_name = self.role_names["head"]
                head_comparison = self._insight9_comparison(
                    validation,
                    reference_validation
                    if isinstance(reference_validation, dict)
                    else {},
                )
                head_state = str(head_comparison["state"])
                comparisons[head_name] = head_comparison
                if head_state == "recalibrate":
                    worst_state = "recalibrate"
                elif head_state == "retry" and worst_state == "pass":
                    worst_state = "retry"
                if head_state != "pass":
                    suspect_roles.append("head")
                    suspect_cameras.append(head_name)
                result = self._result(
                    worst_state,
                    bag_name=bag_name,
                    measurement=measurement,
                    comparisons=comparisons,
                    suspect_roles=suspect_roles,
                    suspect_cameras=suspect_cameras,
                )
                if worst_state == "pass":
                    reference_validation["validated_count"] = int(
                        validation["validation_count"]
                    )
                    self._write_reference(self.reference)
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

        now = time.monotonic()
        poses = {}
        with self._lock:
            sample_sets = {
                role: [
                    (stamp, sample)
                    for stamp, sample in self._samples[self.role_names[role]]
                    if now - stamp <= self.sample_window_sec
                ]
                for role in FIXED_STATION_ROLES
            }
        for role, samples in sample_sets.items():
            name = self.role_names[role]
            thresholds = self._camera_thresholds(name)
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
            if translation_spread > thresholds["stationary_translation_m"]:
                reasons.append(
                    f"{name} is moving ({translation_spread * 1000.0:.1f} mm spread)"
                )
            if rotation_spread > thresholds["stationary_rotation_deg"]:
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
        return {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "poses": poses,
            "insight9_validation": (
                mapper.get("capture_validation") if isinstance(mapper, dict) else None
            ),
        }, []

    @staticmethod
    def _parse_validation_reference(payload: object) -> Optional[dict]:
        if not isinstance(payload, dict) or not payload.get("reference_active"):
            return None
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return None
        try:
            parsed = {
                "session_id": session_id,
                "session_generation": int(payload["session_generation"]),
                "reference_id": int(payload["reference_id"]),
                "reference_keyframe": int(payload["reference_keyframe"]),
                "validated_count": int(payload.get("validation_count", 0)),
            }
        except (KeyError, TypeError, ValueError):
            return None
        return parsed

    @staticmethod
    def _current_validation(measurement: dict) -> tuple[Optional[dict], str]:
        validation = measurement.get("insight9_validation")
        if not isinstance(validation, dict):
            return None, "Insight9 natural-map validation status is unavailable"
        if not validation.get("reference_active"):
            return None, "Insight9 natural-map reference is not active"
        return validation, ""

    @staticmethod
    def _validation_identity_reason(reference: object, current: dict) -> str:
        if not isinstance(reference, dict):
            return "Insight9 validation reference is missing"
        try:
            expected = (
                str(reference.get("session_id") or ""),
                int(reference.get("session_generation", -1)),
                int(reference.get("reference_id", -1)),
                int(reference.get("reference_keyframe", -1)),
            )
            observed = (
                str(current.get("session_id") or ""),
                int(current.get("session_generation", -2)),
                int(current.get("reference_id", -2)),
                int(current.get("reference_keyframe", -2)),
            )
        except (TypeError, ValueError):
            return "Insight9 validation status is malformed; recalibration is required"
        if expected != observed:
            return "Insight9 map session or frozen reference changed; recalibration is required"
        return ""

    def _insight9_comparison(self, validation: dict, reference: dict) -> dict:
        camera_name = self.role_names["head"]
        sequence = int(validation.get("validation_count", 0) or 0)
        validated = int(reference.get("validated_count", 0) or 0)
        last = validation.get("last_validation")
        if sequence <= validated or not isinstance(last, dict):
            return {
                "role": "head",
                "method": "frozen_natural_map_closure",
                "state": "retry",
                "translation_error_m": None,
                "rotation_error_deg": None,
                "threshold_group": "insight9_closure",
                "validation_count": sequence,
                "reason": (
                    "no fresh Insight9 closure; look around the mapped workspace "
                    "and retry"
                ),
            }
        validation_age = float(last.get("age_sec", math.inf))
        if validation_age > self.maximum_insight9_validation_age_sec:
            return {
                "role": "head",
                "method": "frozen_natural_map_closure",
                "state": "retry",
                "translation_error_m": None,
                "rotation_error_deg": None,
                "threshold_group": "insight9_closure",
                "validation_count": sequence,
                "validation_age_sec": round(validation_age, 3),
                "reason": (
                    "Insight9 closure is stale; look around the mapped workspace "
                    "and retry"
                ),
            }
        recent = [
            item
            for item in validation.get("recent_validations", [])
            if isinstance(item, dict)
            and int(item.get("sequence", -1)) > validated
        ]
        if recent and int(recent[0].get("sequence", -1)) > validated + 1:
            return {
                "role": "head",
                "method": "frozen_natural_map_closure",
                "state": "retry",
                "translation_error_m": None,
                "rotation_error_deg": None,
                "threshold_group": "insight9_closure",
                "validation_count": sequence,
                "reason": (
                    "Insight9 validation history is incomplete; set a new batch "
                    "reference before continuing"
                ),
            }
        critical = [
            item
            for item in recent
            if self._comparison_state(
                float(item.get("translation_error_m", math.inf)),
                float(item.get("rotation_error_deg", math.inf)),
                self.thresholds["insight9"],
            )
            == "recalibrate"
        ]
        if critical:
            worst = max(
                critical,
                key=lambda item: max(
                    float(item.get("translation_error_m", math.inf))
                    / self.thresholds["insight9"]["recalibrate_translation_m"],
                    float(item.get("rotation_error_deg", math.inf))
                    / self.thresholds["insight9"]["recalibrate_rotation_deg"],
                ),
            )
            return {
                "role": "head",
                "method": "frozen_natural_map_closure",
                "state": "recalibrate",
                "translation_error_m": round(
                    float(worst.get("translation_error_m", math.inf)), 5
                ),
                "rotation_error_deg": round(
                    float(worst.get("rotation_error_deg", math.inf)), 3
                ),
                "threshold_group": "insight9_closure",
                "validation_count": sequence,
                "reason": (
                    "Insight9 had a critical frozen-map correction since the "
                    "previous PASS"
                ),
            }
        translation_error = float(last.get("translation_error_m", math.inf))
        rotation_error = float(last.get("rotation_error_deg", math.inf))
        state = self._comparison_state(
            translation_error, rotation_error, self.thresholds["insight9"]
        )
        return {
            "role": "head",
            "method": "frozen_natural_map_closure",
            "state": state,
            "translation_error_m": round(translation_error, 5),
            "rotation_error_deg": round(rotation_error, 3),
            "threshold_group": "insight9_closure",
            "validation_count": sequence,
            "validation_age_sec": round(validation_age, 3),
            "reference_keyframe": validation.get("reference_keyframe"),
            "descriptor_matches": last.get("descriptor_matches"),
            "inliers": last.get("inliers"),
            "inlier_ratio": last.get("inlier_ratio"),
            "median_reprojection_error_px": last.get(
                "median_reprojection_error_px"
            ),
            "grid_cells": last.get("grid_cells"),
            "camera": camera_name,
        }

    @staticmethod
    def _comparison_state(
        translation_error: float, rotation_error: float, thresholds: dict
    ) -> str:
        if (
            translation_error > thresholds["recalibrate_translation_m"]
            or rotation_error > thresholds["recalibrate_rotation_deg"]
        ):
            return "recalibrate"
        if (
            translation_error > thresholds["pass_translation_m"]
            or rotation_error > thresholds["pass_rotation_deg"]
        ):
            return "retry"
        return "pass"

    def _threshold_payload(self) -> dict:
        return {
            "insight3": dict(self.thresholds["insight3"]),
            "insight9_closure": {
                key: value
                for key, value in self.thresholds["insight9"].items()
                if key.startswith("pass_") or key.startswith("recalibrate_")
            },
            "minimum_map_points": self.minimum_map_points,
            "maximum_insight9_validation_age_sec": (
                self.maximum_insight9_validation_age_sec
            ),
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
        if not isinstance(payload, dict) or payload.get("version") != 3:
            return None
        if not isinstance(payload.get("poses"), dict) or not isinstance(
            payload.get("insight9_validation"), dict
        ):
            return None
        return payload

    def _write_reference(self, reference: dict) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.reference_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(reference, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.reference_path)

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
