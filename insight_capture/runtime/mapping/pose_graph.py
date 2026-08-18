"""Sparse SE(3) keyframe pose graph with robust loop-closure optimization."""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.sparse import lil_matrix
from scipy.sparse.linalg import lsmr


def _skew(vector: np.ndarray) -> np.ndarray:
    x, y, z = np.asarray(vector, dtype=np.float64).reshape(3)
    return np.array(
        [[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=np.float64
    )


def _so3_exp(rotation_vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(rotation_vector, dtype=np.float64).reshape(3)
    angle = float(np.linalg.norm(vector))
    cross = _skew(vector)
    if angle < 1e-8:
        return np.eye(3) + cross + 0.5 * cross @ cross
    return (
        np.eye(3)
        + math.sin(angle) / angle * cross
        + (1.0 - math.cos(angle)) / (angle * angle) * (cross @ cross)
    )


def _so3_log(rotation: np.ndarray) -> np.ndarray:
    value = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    cosine = float(np.clip((np.trace(value) - 1.0) * 0.5, -1.0, 1.0))
    angle = math.acos(cosine)
    vee = np.array(
        [
            value[2, 1] - value[1, 2],
            value[0, 2] - value[2, 0],
            value[1, 0] - value[0, 1],
        ],
        dtype=np.float64,
    )
    if angle < 1e-8:
        return 0.5 * vee
    if math.pi - angle < 1e-5:
        eigenvalues, eigenvectors = np.linalg.eigh((value + np.eye(3)) * 0.5)
        axis = eigenvectors[:, int(np.argmax(eigenvalues))]
        if float(axis @ vee) < 0.0:
            axis = -axis
        return angle * axis
    return angle / (2.0 * math.sin(angle)) * vee


def se3_exp(tangent: np.ndarray) -> np.ndarray:
    """Return an SE(3) transform from ``[translation, rotation]`` tangent."""

    value = np.asarray(tangent, dtype=np.float64).reshape(6)
    rho = value[:3]
    phi = value[3:]
    angle = float(np.linalg.norm(phi))
    cross = _skew(phi)
    if angle < 1e-8:
        jacobian = np.eye(3) + 0.5 * cross + (cross @ cross) / 6.0
    else:
        angle_sq = angle * angle
        jacobian = (
            np.eye(3)
            + (1.0 - math.cos(angle)) / angle_sq * cross
            + (angle - math.sin(angle)) / (angle_sq * angle) * (cross @ cross)
        )
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = _so3_exp(phi)
    transform[:3, 3] = jacobian @ rho
    return transform


def se3_log(transform: np.ndarray) -> np.ndarray:
    """Return the ``[translation, rotation]`` tangent of an SE(3) transform."""

    value = np.asarray(transform, dtype=np.float64).reshape(4, 4)
    phi = _so3_log(value[:3, :3])
    angle = float(np.linalg.norm(phi))
    cross = _skew(phi)
    if angle < 1e-8:
        inverse_jacobian = np.eye(3) - 0.5 * cross + (cross @ cross) / 12.0
    else:
        half_angle = 0.5 * angle
        coefficient = (
            1.0 - half_angle / math.tan(half_angle)
        ) / (angle * angle)
        inverse_jacobian = np.eye(3) - 0.5 * cross + coefficient * (cross @ cross)
    return np.concatenate((inverse_jacobian @ value[:3, 3], phi))


def _valid_transform(transform: np.ndarray) -> np.ndarray:
    value = np.asarray(transform, dtype=np.float64).reshape(4, 4).copy()
    if not np.all(np.isfinite(value)):
        raise ValueError("pose graph transform contains non-finite values")
    u, _, vt = np.linalg.svd(value[:3, :3])
    value[:3, :3] = u @ vt
    if np.linalg.det(value[:3, :3]) < 0.0:
        u[:, -1] *= -1.0
        value[:3, :3] = u @ vt
    value[3] = (0.0, 0.0, 0.0, 1.0)
    return value


def _inverse_transform(transform: np.ndarray) -> np.ndarray:
    """Invert a rigid transform without invoking a general matrix solver."""

    value = np.asarray(transform, dtype=np.float64).reshape(4, 4)
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = value[:3, :3].T
    result[:3, 3] = -(result[:3, :3] @ value[:3, 3])
    return result


def _rotation_distance_deg(first: np.ndarray, second: np.ndarray) -> float:
    relative = np.asarray(first)[:3, :3].T @ np.asarray(second)[:3, :3]
    return math.degrees(float(np.linalg.norm(_so3_log(relative))))


@dataclass(frozen=True)
class PoseGraphConfig:
    """Noise, robustness, and resource limits for pose graph optimization."""

    odometry_translation_std_m: float = 0.025
    odometry_rotation_std_deg: float = 0.75
    loop_translation_std_m: float = 0.05
    loop_rotation_std_deg: float = 2.0
    robust_delta: float = 2.5
    max_iterations: int = 10
    max_keyframes: int = 600

    def __post_init__(self) -> None:
        positive = (
            self.odometry_translation_std_m,
            self.odometry_rotation_std_deg,
            self.loop_translation_std_m,
            self.loop_rotation_std_deg,
            self.robust_delta,
        )
        if any(not np.isfinite(value) or value <= 0.0 for value in positive):
            raise ValueError("pose graph noise and robust loss values must be positive")
        if self.max_iterations <= 0 or self.max_keyframes < 2:
            raise ValueError("pose graph iteration and keyframe limits are invalid")


@dataclass(frozen=True)
class PoseGraphEdge:
    source_id: int
    target_id: int
    source_to_target: np.ndarray
    translation_std_m: float
    rotation_std_deg: float
    kind: str


@dataclass(frozen=True)
class PoseGraphOptimizationResult:
    optimized: bool
    success: bool
    iterations: int
    initial_cost: float
    final_cost: float
    elapsed_ms: float
    max_translation_correction_m: float
    max_rotation_correction_deg: float


class KeyframePoseGraph:
    """Keep VIO keyframes and distribute accepted global loop corrections."""

    def __init__(self, config: Optional[PoseGraphConfig] = None) -> None:
        self.config = config or PoseGraphConfig()
        self._ids: list[int] = []
        self._stamps_ns: list[int] = []
        self._odom_poses: dict[int, np.ndarray] = {}
        self._map_poses: dict[int, np.ndarray] = {}
        self._edges: list[PoseGraphEdge] = []
        self._loop_edges = 0

    @property
    def keyframe_count(self) -> int:
        return len(self._ids)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    @property
    def loop_edge_count(self) -> int:
        return self._loop_edges

    @property
    def full(self) -> bool:
        return len(self._ids) >= self.config.max_keyframes

    def clear(self) -> None:
        self._ids.clear()
        self._stamps_ns.clear()
        self._odom_poses.clear()
        self._map_poses.clear()
        self._edges.clear()
        self._loop_edges = 0

    def add_keyframe(
        self,
        keyframe_id: int,
        stamp_ns: int,
        odom_pose: np.ndarray,
        *,
        initial_map_pose: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Add one node and a VIO relative-pose edge from its predecessor."""

        keyframe_id = int(keyframe_id)
        stamp_ns = int(stamp_ns)
        if keyframe_id in self._map_poses:
            raise ValueError(f"duplicate pose graph keyframe {keyframe_id}")
        if self.full:
            raise OverflowError("pose graph keyframe capacity reached")
        if self._stamps_ns and stamp_ns <= self._stamps_ns[-1]:
            raise ValueError("pose graph keyframe timestamps must increase")
        odom = _valid_transform(odom_pose)
        if not self._ids:
            mapped = _valid_transform(
                odom if initial_map_pose is None else initial_map_pose
            )
        else:
            previous_id = self._ids[-1]
            previous_odom = self._odom_poses[previous_id]
            relative = _inverse_transform(previous_odom) @ odom
            mapped = self._map_poses[previous_id] @ relative
            self._edges.append(
                PoseGraphEdge(
                    source_id=previous_id,
                    target_id=keyframe_id,
                    source_to_target=relative,
                    translation_std_m=self.config.odometry_translation_std_m,
                    rotation_std_deg=self.config.odometry_rotation_std_deg,
                    kind="odometry",
                )
            )
        self._ids.append(keyframe_id)
        self._stamps_ns.append(stamp_ns)
        self._odom_poses[keyframe_id] = odom
        self._map_poses[keyframe_id] = mapped
        return mapped.copy()

    def add_global_loop_edge(
        self, target_id: int, target_map_pose: np.ndarray
    ) -> PoseGraphEdge:
        """Constrain a keyframe absolute pose through an edge from the fixed anchor."""

        if not self._ids:
            raise ValueError("cannot add a loop edge to an empty pose graph")
        target_id = int(target_id)
        if target_id not in self._map_poses:
            raise KeyError(f"unknown pose graph keyframe {target_id}")
        anchor_id = self._ids[0]
        relative = _inverse_transform(self._map_poses[anchor_id]) @ _valid_transform(
            target_map_pose
        )
        edge = PoseGraphEdge(
            source_id=anchor_id,
            target_id=target_id,
            source_to_target=relative,
            translation_std_m=self.config.loop_translation_std_m,
            rotation_std_deg=self.config.loop_rotation_std_deg,
            kind="loop",
        )
        self._edges.append(edge)
        self._loop_edges += 1
        return edge

    def pose(self, keyframe_id: int) -> np.ndarray:
        return self._map_poses[int(keyframe_id)].copy()

    def pose_snapshot(self) -> dict[int, np.ndarray]:
        return {key: value.copy() for key, value in self._map_poses.items()}

    def correction_for_keyframe(self, keyframe_id: int) -> np.ndarray:
        keyframe_id = int(keyframe_id)
        return self._map_poses[keyframe_id] @ _inverse_transform(
            self._odom_poses[keyframe_id]
        )

    def correction_at(self, stamp_ns: int) -> Optional[np.ndarray]:
        """Interpolate optimized map-to-odom corrections along graph time."""

        if not self._ids:
            return None
        stamp_ns = int(stamp_ns)
        index = bisect.bisect_left(self._stamps_ns, stamp_ns)
        if index <= 0:
            return self.correction_for_keyframe(self._ids[0])
        if index >= len(self._ids):
            return self.correction_for_keyframe(self._ids[-1])
        first_id = self._ids[index - 1]
        second_id = self._ids[index]
        first_stamp = self._stamps_ns[index - 1]
        second_stamp = self._stamps_ns[index]
        fraction = (stamp_ns - first_stamp) / float(second_stamp - first_stamp)
        first = self.correction_for_keyframe(first_id)
        second = self.correction_for_keyframe(second_id)
        delta = se3_log(_inverse_transform(first) @ second)
        return first @ se3_exp(fraction * delta)

    def optimize(self) -> PoseGraphOptimizationResult:
        """Run sparse robust least squares while keeping the first pose fixed."""

        started = time.perf_counter()
        if len(self._ids) < 2 or self._loop_edges == 0:
            return PoseGraphOptimizationResult(
                optimized=False,
                success=False,
                iterations=0,
                initial_cost=0.0,
                final_cost=0.0,
                elapsed_ms=0.0,
                max_translation_correction_m=0.0,
                max_rotation_correction_deg=0.0,
            )

        variable_ids = self._ids[1:]
        offsets = {
            keyframe_id: index * 6
            for index, keyframe_id in enumerate(variable_ids)
        }
        base = self.pose_snapshot()
        optimized_poses = self.pose_snapshot()

        def edge_error(edge: PoseGraphEdge, poses: dict[int, np.ndarray]) -> np.ndarray:
            predicted = _inverse_transform(poses[edge.source_id]) @ poses[edge.target_id]
            return se3_log(_inverse_transform(edge.source_to_target) @ predicted)

        def scaled_residuals(poses: dict[int, np.ndarray]) -> np.ndarray:
            values = np.empty((len(self._edges), 6), dtype=np.float64)
            for index, edge in enumerate(self._edges):
                error = edge_error(edge, poses)
                values[index, :3] = error[:3] / edge.translation_std_m
                values[index, 3:] = error[3:] / math.radians(
                    edge.rotation_std_deg
                )
            return values.reshape(-1)

        def robust_cost(residuals: np.ndarray) -> float:
            edge_residuals = np.asarray(residuals, dtype=np.float64).reshape(-1, 6)
            normalized_sq = np.sum(edge_residuals * edge_residuals, axis=1) / 6.0
            delta_sq = self.config.robust_delta**2
            return 3.0 * delta_sq * float(
                np.sum(np.log1p(normalized_sq / delta_sq))
            )

        initial_residuals = scaled_residuals(optimized_poses)
        initial_cost = robust_cost(initial_residuals)
        final_cost = initial_cost
        iterations = 0
        converged = False
        for iteration in range(self.config.max_iterations):
            jacobian = lil_matrix(
                (len(self._edges) * 6, len(variable_ids) * 6),
                dtype=np.float64,
            )
            target = np.empty((len(self._edges) * 6,), dtype=np.float64)
            for edge_index, edge in enumerate(self._edges):
                row = edge_index * 6
                error = edge_error(edge, optimized_poses)
                scale = np.array(
                    [1.0 / edge.translation_std_m] * 3
                    + [1.0 / math.radians(edge.rotation_std_deg)] * 3,
                    dtype=np.float64,
                )
                normalized = error * scale
                norm = float(np.linalg.norm(normalized) / math.sqrt(6.0))
                # Cauchy IRLS suppresses a geometrically accepted but globally
                # inconsistent loop more strongly than Huber. This is a second
                # line of defense behind descriptor, PnP, and temporal gates.
                robust_weight = 1.0 / math.sqrt(
                    1.0 + (norm / self.config.robust_delta) ** 2
                )
                weighted_scale = robust_weight * scale
                target[row : row + 6] = -weighted_scale * error

                # Right-pose perturbations give these first-order SE(3)
                # Jacobians near a satisfied edge. Re-linearizing after every
                # solve handles larger loop corrections without a dense finite-
                # difference pass over the full graph.
                relative_inverse = _inverse_transform(edge.source_to_target)
                rotation = relative_inverse[:3, :3]
                translation = relative_inverse[:3, 3]
                adjoint = np.zeros((6, 6), dtype=np.float64)
                adjoint[:3, :3] = rotation
                adjoint[:3, 3:] = _skew(translation) @ rotation
                adjoint[3:, 3:] = rotation
                source_jacobian = -adjoint
                target_jacobian = np.eye(6, dtype=np.float64)
                for keyframe_id, block in (
                    (edge.source_id, source_jacobian),
                    (edge.target_id, target_jacobian),
                ):
                    offset = offsets.get(keyframe_id)
                    if offset is None:
                        continue
                    jacobian[
                        row : row + 6, offset : offset + 6
                    ] = weighted_scale[:, None] * block

            solution = lsmr(
                jacobian.tocsr(),
                target,
                atol=1e-6,
                btol=1e-6,
                maxiter=max(50, len(variable_ids) * 2),
            )
            step = np.asarray(solution[0], dtype=np.float64)
            if not np.isfinite(step).all():
                break
            max_step = float(
                max(
                    np.linalg.norm(step[index : index + 6])
                    for index in range(0, len(step), 6)
                )
            )
            accepted = False
            for step_scale in (1.0, 0.5, 0.25, 0.125):
                candidate = {self._ids[0]: optimized_poses[self._ids[0]]}
                for keyframe_id in variable_ids:
                    offset = offsets[keyframe_id]
                    candidate[keyframe_id] = optimized_poses[keyframe_id] @ se3_exp(
                        step_scale * step[offset : offset + 6]
                    )
                candidate_residuals = scaled_residuals(candidate)
                candidate_cost = robust_cost(candidate_residuals)
                if np.isfinite(candidate_cost) and candidate_cost < final_cost:
                    optimized_poses = candidate
                    final_cost = candidate_cost
                    accepted = True
                    break
            iterations = iteration + 1
            if not accepted:
                break
            if max_step < 1e-6:
                converged = True
                break

        finite = all(np.isfinite(pose).all() for pose in optimized_poses.values())
        improved = finite and final_cost < initial_cost
        max_translation = 0.0
        max_rotation = 0.0
        if improved:
            for keyframe_id in variable_ids:
                before = base[keyframe_id]
                # SE(3) right updates preserve an orthonormal rotation; avoid a
                # per-node SVD here because it dominates loop-closure latency on
                # Jetson for large graphs.
                after = optimized_poses[keyframe_id].copy()
                after[3] = (0.0, 0.0, 0.0, 1.0)
                max_translation = max(
                    max_translation,
                    float(np.linalg.norm(after[:3, 3] - before[:3, 3])),
                )
                max_rotation = max(
                    max_rotation, _rotation_distance_deg(before, after)
                )
                self._map_poses[keyframe_id] = after
        return PoseGraphOptimizationResult(
            optimized=improved,
            success=bool((converged or improved) and finite),
            iterations=iterations,
            initial_cost=initial_cost,
            final_cost=final_cost,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            max_translation_correction_m=max_translation,
            max_rotation_correction_deg=max_rotation,
        )
