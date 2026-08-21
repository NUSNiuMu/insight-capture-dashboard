"""当前会话内的稀疏地标确认、体素去重和有界存储。

每帧双目都可能产生动态物体、误匹配或深度抖动点。本模块不做跨帧描述子跟踪，
而是用世界坐标体素作为轻量关联：同一体素在不同关键帧重复出现达到门限后才成为
确认地标。该设计适合 Jetson 在线运行，但只是观测融合，不是联合优化三维点的 BA。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np


VoxelKey = Tuple[int, int, int]


@dataclass(frozen=True)
class LandmarkMapConfig:
    """瞬态点过滤、体素尺寸和地图资源上限。"""

    voxel_size_m: float = 0.04
    confirmation_observations: int = 3
    candidate_ttl_keyframes: int = 12
    max_landmarks: int = 100_000

    def __post_init__(self) -> None:
        if self.voxel_size_m <= 0.0:
            raise ValueError("voxel_size_m must be positive")
        if self.confirmation_observations < 2:
            raise ValueError("confirmation_observations must be at least two")
        if self.candidate_ttl_keyframes < self.confirmation_observations:
            raise ValueError("candidate TTL must cover the confirmation window")
        if self.max_landmarks <= 0:
            raise ValueError("max_landmarks must be positive")


@dataclass
class _Landmark:
    """体素内的位置、描述子运行均值及观测生命周期。"""

    position: np.ndarray
    descriptor: Optional[np.ndarray]
    observations: int
    first_keyframe: int
    last_keyframe: int
    score: float


@dataclass(frozen=True)
class MapUpdate:
    """一次关键帧写入后的统计摘要，用于 mapper 状态诊断。"""

    input_points: int
    unique_voxels: int
    promoted: int
    confirmed_total: int
    candidates_total: int


class LandmarkMap:
    """只确认在多个关键帧中落入同一世界体素的点。"""

    def __init__(self, config: Optional[LandmarkMapConfig] = None) -> None:
        self.config = config or LandmarkMapConfig()
        self._candidates: Dict[VoxelKey, _Landmark] = {}
        self._confirmed: Dict[VoxelKey, _Landmark] = {}

    def clear(self) -> None:
        self._candidates.clear()
        self._confirmed.clear()

    def update(
        self,
        keyframe_id: int,
        points_world: np.ndarray,
        *,
        descriptors: Optional[np.ndarray] = None,
        scores: Optional[np.ndarray] = None,
    ) -> MapUpdate:
        """Insert a keyframe, counting at most one observation per voxel."""

        points = np.asarray(points_world, dtype=np.float64).reshape(-1, 3)
        if descriptors is not None:
            descriptors = np.asarray(descriptors, dtype=np.float32)
            if descriptors.ndim != 2 or descriptors.shape[0] != len(points):
                raise ValueError("descriptors must have shape N x D")
        if scores is None:
            scores_array = np.ones((len(points),), dtype=np.float32)
        else:
            scores_array = np.asarray(scores, dtype=np.float32).reshape(-1)
            if len(scores_array) != len(points):
                raise ValueError("scores must have one value per point")

        finite = np.isfinite(points).all(axis=1)
        points = points[finite]
        scores_array = scores_array[finite]
        if descriptors is not None:
            descriptors = descriptors[finite]

        # 单帧同一体素最多计一次观测，防止密集纹理在一帧内伪造“重复确认”。
        representatives: Dict[VoxelKey, int] = {}
        if len(points):
            keys = np.floor(points / self.config.voxel_size_m).astype(np.int64)
            for index, raw_key in enumerate(keys):
                key = tuple(int(value) for value in raw_key)
                previous = representatives.get(key)
                if previous is None or scores_array[index] > scores_array[previous]:
                    representatives[key] = index

        promoted = 0
        for key, index in representatives.items():
            descriptor = None if descriptors is None else descriptors[index]
            if key in self._confirmed:
                # 已确认地标继续更新运行均值，但不会重新计入 promoted。
                self._merge(
                    self._confirmed[key],
                    points[index],
                    descriptor,
                    float(scores_array[index]),
                    int(keyframe_id),
                )
                continue

            # 首次出现先进入候选区；只有不同关键帧的重复观测才能晋升。
            candidate = self._candidates.get(key)
            if candidate is None:
                self._candidates[key] = _Landmark(
                    position=points[index].copy(),
                    descriptor=self._normalized_descriptor(descriptor),
                    observations=1,
                    first_keyframe=int(keyframe_id),
                    last_keyframe=int(keyframe_id),
                    score=float(scores_array[index]),
                )
                continue
            if candidate.last_keyframe == int(keyframe_id):
                continue
            self._merge(
                candidate,
                points[index],
                descriptor,
                float(scores_array[index]),
                int(keyframe_id),
            )
            if candidate.observations >= self.config.confirmation_observations:
                if len(self._confirmed) < self.config.max_landmarks:
                    self._confirmed[key] = candidate
                    promoted += 1
                del self._candidates[key]

        # 长时间未复现的候选大概率是动态物体或偶发误匹配，主动释放内存。
        oldest = int(keyframe_id) - self.config.candidate_ttl_keyframes
        self._candidates = {
            key: value
            for key, value in self._candidates.items()
            if value.last_keyframe >= oldest
        }
        return MapUpdate(
            input_points=len(points),
            unique_voxels=len(representatives),
            promoted=promoted,
            confirmed_total=len(self._confirmed),
            candidates_total=len(self._candidates),
        )

    @staticmethod
    def _normalized_descriptor(descriptor: Optional[np.ndarray]) -> Optional[np.ndarray]:
        if descriptor is None:
            return None
        value = np.asarray(descriptor, dtype=np.float32).copy()
        norm = float(np.linalg.norm(value))
        return value / norm if norm > 1e-12 else value

    def _merge(
        self,
        landmark: _Landmark,
        position: np.ndarray,
        descriptor: Optional[np.ndarray],
        score: float,
        keyframe_id: int,
    ) -> None:
        # 位置采用在线算术平均；描述子逐次归一化，避免观测数增加导致模长漂移。
        next_count = landmark.observations + 1
        landmark.position += (position - landmark.position) / next_count
        normalized = self._normalized_descriptor(descriptor)
        if normalized is not None:
            if landmark.descriptor is None:
                landmark.descriptor = normalized
            else:
                landmark.descriptor = self._normalized_descriptor(
                    landmark.descriptor + normalized
                )
        landmark.observations = next_count
        landmark.last_keyframe = keyframe_id
        landmark.score = max(landmark.score, score)

    def points(self) -> np.ndarray:
        if not self._confirmed:
            return np.empty((0, 3), dtype=np.float32)
        return np.asarray(
            [landmark.position for landmark in self._confirmed.values()],
            dtype=np.float32,
        )

    def confirmed_count(self) -> int:
        """Return the confirmed landmark count without materializing point data."""

        return len(self._confirmed)

    def descriptors(
        self, *, max_source_keyframe: Optional[int] = None
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Return descriptor landmarks, optionally excluding recently created points."""

        entries = [
            landmark
            for landmark in self._confirmed.values()
            if landmark.descriptor is not None
            and (
                max_source_keyframe is None
                or landmark.first_keyframe <= int(max_source_keyframe)
            )
        ]
        if not entries:
            return (
                np.empty((0, 3), dtype=np.float32),
                np.empty((0, 0), dtype=np.float32),
            )
        return (
            np.asarray([entry.position for entry in entries], dtype=np.float32),
            np.asarray([entry.descriptor for entry in entries], dtype=np.float32),
        )
