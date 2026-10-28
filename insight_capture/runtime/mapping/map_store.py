"""Bounded descriptor landmarks with spatial indexing and multi-frame confirmation."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Dict, Optional, Tuple

import numpy as np


VoxelKey = Tuple[int, int, int]
_NEIGHBORS = tuple(product((-1, 0, 1), repeat=3))


@dataclass(frozen=True)
class LandmarkMapConfig:
    """Spatial association, descriptor agreement and storage limits."""

    voxel_size_m: float = 0.04
    confirmation_observations: int = 3
    candidate_ttl_keyframes: int = 12
    max_landmarks: int = 100_000
    association_min_similarity: float = 0.80
    association_ratio_test: float = 0.80

    def __post_init__(self) -> None:
        if not np.isfinite(self.voxel_size_m) or self.voxel_size_m <= 0.0:
            raise ValueError("voxel_size_m must be finite and positive")
        if self.confirmation_observations < 2:
            raise ValueError("confirmation_observations must be at least two")
        if self.candidate_ttl_keyframes < self.confirmation_observations:
            raise ValueError("candidate TTL must cover the confirmation window")
        if self.max_landmarks <= 0:
            raise ValueError("max_landmarks must be positive")
        if not 0.0 < self.association_min_similarity <= 1.0:
            raise ValueError("association similarity must be in (0, 1]")
        if not 0.0 < self.association_ratio_test < 1.0:
            raise ValueError("association ratio must be in (0, 1)")


@dataclass
class _Landmark:
    position: np.ndarray
    voxel: VoxelKey
    descriptor: Optional[np.ndarray]
    descriptor_weight: float
    observations: int
    first_keyframe: int
    last_keyframe: int
    score: float


@dataclass(frozen=True)
class MapUpdate:
    input_points: int
    unique_voxels: int
    promoted: int
    confirmed_total: int
    candidates_total: int


class LandmarkMap:
    """Associate distinct feature tracks inside neighboring spatial buckets."""

    def __init__(self, config: Optional[LandmarkMapConfig] = None) -> None:
        self.config = config or LandmarkMapConfig()
        self._landmarks: Dict[int, _Landmark] = {}
        self._voxels: Dict[VoxelKey, set[int]] = {}
        self._candidates: set[int] = set()
        self._confirmed: set[int] = set()
        self._next_id = 0

    def clear(self) -> None:
        self._landmarks.clear()
        self._voxels.clear()
        self._candidates.clear()
        self._confirmed.clear()
        self._next_id = 0

    def _key(self, point: np.ndarray) -> VoxelKey:
        return tuple(int(value) for value in np.floor(point / self.config.voxel_size_m))

    def _remove_from_voxel(self, key: VoxelKey, landmark_id: int) -> None:
        bucket = self._voxels[key]
        bucket.remove(landmark_id)
        if not bucket:
            del self._voxels[key]

    def _associate(
        self, point: np.ndarray, descriptor: Optional[np.ndarray], key: VoxelKey
    ) -> Optional[int]:
        best_id = None
        best_rank = None
        second_rank = None
        radius_sq = self.config.voxel_size_m**2
        x, y, z = key
        for dx, dy, dz in _NEIGHBORS:
            neighbor = (x + dx, y + dy, z + dz)
            for landmark_id in self._voxels.get(neighbor, ()):
                landmark = self._landmarks[landmark_id]
                delta = landmark.position - point
                distance_sq = float(delta @ delta)
                if distance_sq > radius_sq:
                    continue
                if descriptor is None and landmark.descriptor is None:
                    similarity = 1.0
                elif descriptor is None or landmark.descriptor is None:
                    continue
                else:
                    similarity = float(landmark.descriptor @ descriptor)
                rank = (-similarity, distance_sq, landmark_id)
                if best_rank is None or rank < best_rank:
                    second_rank = best_rank
                    best_id, best_rank = landmark_id, rank
                elif second_rank is None or rank < second_rank:
                    second_rank = rank
        if best_rank is not None and descriptor is not None:
            if -best_rank[0] < self.config.association_min_similarity:
                return None
            # Nearby repeated texture must not silently join distinct feature tracks.
            if second_rank is not None and (
                max(0.0, 1.0 + best_rank[0])
                >= self.config.association_ratio_test**2 * max(0.0, 1.0 + second_rank[0])
            ):
                return None
        return best_id

    def update(
        self,
        keyframe_id: int,
        points_world: np.ndarray,
        *,
        descriptors: Optional[np.ndarray] = None,
        scores: Optional[np.ndarray] = None,
    ) -> MapUpdate:
        """Fuse at most one observation per feature track and keyframe."""

        points = np.asarray(points_world, dtype=np.float64).reshape(-1, 3)
        if descriptors is not None:
            descriptors = np.asarray(descriptors, dtype=np.float32)
            if descriptors.ndim != 2 or descriptors.shape[0] != len(points):
                raise ValueError("descriptors must have shape N x D")
        scores_array = (
            np.ones(len(points), dtype=np.float32)
            if scores is None
            else np.asarray(scores, dtype=np.float32).reshape(-1)
        )
        if len(scores_array) != len(points):
            raise ValueError("scores must have one value per point")
        finite = np.isfinite(points).all(axis=1) & np.isfinite(scores_array)
        if descriptors is not None:
            finite &= np.isfinite(descriptors).all(axis=1)
        points, scores_array = points[finite], scores_array[finite]
        if descriptors is not None:
            descriptors = descriptors[finite]

        oldest = int(keyframe_id) - self.config.candidate_ttl_keyframes
        expired = [
            index for index in self._candidates
            if self._landmarks[index].last_keyframe < oldest
        ]
        for index in expired:
            self._remove_from_voxel(self._landmarks[index].voxel, index)
            del self._landmarks[index]
            self._candidates.remove(index)

        promoted = 0
        input_voxels = set()
        for index in np.argsort(-scores_array, kind="stable"):
            point = points[index]
            key = self._key(point)
            input_voxels.add(key)
            descriptor = self._normalized_descriptor(
                None if descriptors is None else descriptors[index]
            )
            landmark_id = self._associate(point, descriptor, key)
            if landmark_id is None:
                if len(self._landmarks) >= self.config.max_landmarks:
                    continue
                landmark_id = self._next_id
                self._next_id += 1
                self._landmarks[landmark_id] = _Landmark(
                    position=point.copy(), voxel=key, descriptor=descriptor,
                    descriptor_weight=0.0 if descriptor is None else 1.0,
                    observations=1, first_keyframe=int(keyframe_id),
                    last_keyframe=int(keyframe_id), score=float(scores_array[index]),
                )
                self._voxels.setdefault(key, set()).add(landmark_id)
                self._candidates.add(landmark_id)
                continue
            landmark = self._landmarks[landmark_id]
            if landmark.last_keyframe >= int(keyframe_id):
                continue
            previous_key = landmark.voxel
            self._merge(landmark, point, descriptor, float(scores_array[index]), keyframe_id)
            updated_key = self._key(landmark.position)
            if previous_key != updated_key:
                self._remove_from_voxel(previous_key, landmark_id)
                self._voxels.setdefault(updated_key, set()).add(landmark_id)
                landmark.voxel = updated_key
            if landmark_id in self._candidates and landmark.observations >= self.config.confirmation_observations:
                self._candidates.remove(landmark_id)
                self._confirmed.add(landmark_id)
                promoted += 1
        return MapUpdate(
            input_points=len(points), unique_voxels=len(input_voxels), promoted=promoted,
            confirmed_total=len(self._confirmed), candidates_total=len(self._candidates),
        )

    @staticmethod
    def _normalized_descriptor(descriptor: Optional[np.ndarray]) -> Optional[np.ndarray]:
        if descriptor is None:
            return None
        value = np.asarray(descriptor, dtype=np.float32).copy()
        norm = float(np.linalg.norm(value))
        return value / norm if norm > 1e-12 else None

    def _merge(
        self, landmark: _Landmark, position: np.ndarray,
        descriptor: Optional[np.ndarray], score: float, keyframe_id: int,
    ) -> None:
        next_count = landmark.observations + 1
        landmark.position += (position - landmark.position) / next_count
        if descriptor is not None:
            # Keep the magnitude of the descriptor sum so every frame has equal weight.
            total = landmark.descriptor * landmark.descriptor_weight + descriptor
            landmark.descriptor_weight = float(np.linalg.norm(total))
            landmark.descriptor = total / landmark.descriptor_weight
        landmark.observations = next_count
        landmark.last_keyframe = int(keyframe_id)
        landmark.score = max(landmark.score, score)

    def points(self) -> np.ndarray:
        return np.asarray(
            [self._landmarks[index].position for index in sorted(self._confirmed)],
            dtype=np.float32,
        ).reshape(-1, 3)

    def confirmed_count(self) -> int:
        return len(self._confirmed)

    def descriptors(
        self, *, max_source_keyframe: Optional[int] = None
    ) -> Tuple[np.ndarray, np.ndarray]:
        entries = [
            self._landmarks[index] for index in sorted(self._confirmed)
            if self._landmarks[index].descriptor is not None
            and (max_source_keyframe is None
                 or self._landmarks[index].first_keyframe <= int(max_source_keyframe))
        ]
        # Voxels accelerate association; distinct confirmed feature identities
        # in the same bucket must remain available for 2D-3D localization.
        if not entries:
            return np.empty((0, 3), dtype=np.float32), np.empty((0, 0), dtype=np.float32)
        return (
            np.asarray([entry.position for entry in entries], dtype=np.float32),
            np.asarray([entry.descriptor for entry in entries], dtype=np.float32),
        )
