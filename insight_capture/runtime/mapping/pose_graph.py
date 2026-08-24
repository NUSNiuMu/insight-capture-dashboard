"""带鲁棒回环约束的稀疏 SE(3) 关键帧位姿图。

图节点只包含关键帧位姿，边包括相邻关键帧的 VIO 相对运动和 PnP 给出的全局回环。
优化变量不包含三维地标和像素观测，所以这是位姿图优化而非 Bundle Adjustment。
首个关键帧固定以消除全局规范自由度，其余节点以右乘 SE(3) 增量迭代更新。
"""

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
    """把 ``[平移, 旋转向量]`` 六维切向量映射为 SE(3) 变换。"""

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
    """把 SE(3) 变换映射为 ``[平移, 旋转向量]`` 六维切向量。"""

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
    """各类边的噪声、鲁棒核参数和在线图资源上限。"""

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
    """一条相对位姿约束；标准差决定平移与旋转残差的归一化权重。"""

    source_id: int
    target_id: int
    source_to_target: np.ndarray
    translation_std_m: float
    rotation_std_deg: float
    kind: str


@dataclass(frozen=True)
class PoseGraphOptimizationResult:
    """一次优化的收敛、代价、耗时和最大位姿改变量。"""

    optimized: bool
    success: bool
    iterations: int
    initial_cost: float
    final_cost: float
    elapsed_ms: float
    max_translation_correction_m: float
    max_rotation_correction_deg: float


class KeyframePoseGraph:
    """保存 VIO 关键帧，并把已接受的全局回环修正分布到整段轨迹。"""

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
        """新增节点，并从前一节点添加一条 VIO 相对位姿边。"""

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
            # 首节点作为固定锚点；后续优化不会为它分配变量块。
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
        """把 PnP 全局位姿改写为固定首节点到当前节点的回环边。"""

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

    def keyframe_ids(self) -> tuple[int, ...]:
        """Return graph node IDs in timestamp order."""

        return tuple(self._ids)

    def apply_pose_updates(self, poses: dict[int, np.ndarray]) -> None:
        """Apply externally optimized map poses without changing graph measurements.

        Local bundle adjustment works on a background snapshot. The mapper validates
        that snapshot before using this method, while this class validates node IDs
        and rigid transforms so a malformed solution cannot partially update the graph.
        """

        unknown = set(int(key) for key in poses) - set(self._map_poses)
        if unknown:
            raise KeyError(f"unknown pose graph keyframes: {sorted(unknown)}")
        validated = {
            int(keyframe_id): _valid_transform(pose)
            for keyframe_id, pose in poses.items()
        }
        self._map_poses.update(validated)

    def correction_for_keyframe(self, keyframe_id: int) -> np.ndarray:
        keyframe_id = int(keyframe_id)
        return self._map_poses[keyframe_id] @ _inverse_transform(
            self._odom_poses[keyframe_id]
        )

    def correction_at(self, stamp_ns: int) -> Optional[np.ndarray]:
        """沿关键帧时间轴在 SE(3) 上插值优化后的 ``T_map_odom``。"""

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
        """固定首节点，运行稀疏鲁棒最小二乘。

        每次迭代重新线性化所有边，通过 Cauchy IRLS 降低异常回环权重，再用稀疏
        LSMR 求解增量。候选步长必须实际降低鲁棒代价才会被接受。
        """

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

        # 首节点固定后，每个剩余节点对应连续六列优化变量。
        variable_ids = self._ids[1:]
        offsets = {
            keyframe_id: index * 6
            for index, keyframe_id in enumerate(variable_ids)
        }
        base = self.pose_snapshot()
        optimized_poses = self.pose_snapshot()

        def edge_error(edge: PoseGraphEdge, poses: dict[int, np.ndarray]) -> np.ndarray:
            # 比较图中当前预测相对位姿与边测量，并映射到六维切空间。
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
                # Cauchy IRLS 比 Huber 更强地抑制“局部 PnP 通过但全图不一致”的回环；
                # 这是描述子、PnP 和时序门限之后的第二道防线。
                robust_weight = 1.0 / math.sqrt(
                    1.0 + (norm / self.config.robust_delta) ** 2
                )
                weighted_scale = robust_weight * scale
                target[row : row + 6] = -weighted_scale * error

                # 右乘位姿扰动在已接近满足的边附近给出以下一阶 Jacobian。每轮重新
                # 线性化可处理较大修正，同时避免对全图做稠密有限差分。
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
            # 简单回溯线搜索防止一阶线性化在大回环下跨过局部最优点。
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
                # SE(3) 右乘更新天然保持旋转正交；不再逐节点 SVD，因为大图上该操作
                # 会主导 Jetson 的回环延迟。
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
