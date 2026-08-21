"""内部验证使用的校正双目稠密重建与有界体素融合。

该模块只被 ``tools/mapping_validation/dense_mapper.py`` 调用，不参与客户在线稀疏
建图。它使用 CPU StereoSGBM 验证标定、视差和空间几何，不能与生产 SuperPoint/
SuperGlue 稀疏地标路径混为一谈。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

import cv2
import numpy as np

from .geometry import StereoCalibration


VoxelKey = Tuple[int, int, int]


@dataclass(frozen=True)
class DenseStereoConfig:
    """CPU StereoSGBM 的视差范围、块大小、深度范围和输出采样上限。"""

    min_disparity_px: float = 1.0
    num_disparities: int = 128
    block_size: int = 5
    min_depth_m: float = 0.25
    max_depth_m: float = 6.0
    pixel_stride: int = 2
    max_points: int = 80_000

    def __post_init__(self) -> None:
        if self.num_disparities <= 0 or self.num_disparities % 16:
            raise ValueError("num_disparities must be a positive multiple of 16")
        if self.block_size < 3 or self.block_size % 2 == 0:
            raise ValueError("block_size must be odd and at least three")
        if self.min_depth_m <= 0.0 or self.max_depth_m <= self.min_depth_m:
            raise ValueError("invalid dense depth range")
        if self.pixel_stride <= 0 or self.max_points <= 0:
            raise ValueError("pixel_stride and max_points must be positive")


@dataclass(frozen=True)
class DenseStereoResult:
    """左目坐标系中的稠密点及本帧视差/深度诊断。"""

    points_left: np.ndarray
    valid_pixels: int
    median_depth_m: float | None
    median_disparity_px: float | None


class DenseStereoEstimator:
    """从已校正 mono8 双目图估计经过深度过滤的稠密点云。"""

    def __init__(self, config: DenseStereoConfig | None = None) -> None:
        self.config = config or DenseStereoConfig()
        block_area = self.config.block_size * self.config.block_size
        self._matcher = cv2.StereoSGBM_create(
            minDisparity=0,
            numDisparities=self.config.num_disparities,
            blockSize=self.config.block_size,
            P1=8 * block_area,
            P2=32 * block_area,
            disp12MaxDiff=1,
            preFilterCap=31,
            uniquenessRatio=10,
            speckleWindowSize=100,
            speckleRange=2,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
        )
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

    def reconstruct(
        self,
        left_gray: np.ndarray,
        right_gray: np.ndarray,
        calibration: StereoCalibration,
    ) -> DenseStereoResult:
        """Compute disparity and reproject valid regularly sampled pixels."""

        left = np.asarray(left_gray, dtype=np.uint8)
        right = np.asarray(right_gray, dtype=np.uint8)
        expected_shape = (calibration.height, calibration.width)
        if left.shape != expected_shape or right.shape != expected_shape:
            raise ValueError(
                f"dense stereo images must have shape {expected_shape}, "
                f"got {left.shape} and {right.shape}"
            )
        # 红外图局部对比度差异会削弱块匹配，CLAHE 只用于视差计算而不改原图。
        enhanced_left = self._clahe.apply(left)
        enhanced_right = self._clahe.apply(right)
        disparity = self._matcher.compute(enhanced_left, enhanced_right).astype(
            np.float32
        )
        disparity /= 16.0

        stride = self.config.pixel_stride
        rows = np.arange(0, calibration.height, stride, dtype=np.int32)
        columns = np.arange(0, calibration.width, stride, dtype=np.int32)
        uu, vv = np.meshgrid(columns, rows)
        sampled_disparity = disparity[vv, uu]

        left_projection = calibration.left_projection
        right_projection = calibration.right_projection
        fx = float(left_projection[0, 0])
        fy = float(left_projection[1, 1])
        cx = float(left_projection[0, 2])
        cy = float(left_projection[1, 2])
        right_cx = float(right_projection[0, 2])
        # 两个投影矩阵主点不完全一致时，深度公式必须使用修正后的有效视差。
        effective_disparity = sampled_disparity - (cx - right_cx)
        disparity_valid = (
            np.isfinite(effective_disparity)
            & (effective_disparity >= self.config.min_disparity_px)
        )
        depth = np.full_like(effective_disparity, np.nan, dtype=np.float32)
        np.divide(
            fx * calibration.baseline_m,
            effective_disparity,
            out=depth,
            where=disparity_valid,
        )
        valid = (
            disparity_valid
            & np.isfinite(depth)
            & (depth >= self.config.min_depth_m)
            & (depth <= self.config.max_depth_m)
        )
        valid_u = uu[valid].astype(np.float32)
        valid_v = vv[valid].astype(np.float32)
        valid_depth = depth[valid].astype(np.float32)
        valid_disparity = effective_disparity[valid].astype(np.float32)
        points = np.column_stack(
            (
                (valid_u - cx) * valid_depth / fx,
                (valid_v - cy) * valid_depth / fy,
                valid_depth,
            )
        ).astype(np.float32)
        if len(points) > self.config.max_points:
            indices = np.linspace(
                0, len(points) - 1, self.config.max_points, dtype=np.int64
            )
            points = points[indices]

        return DenseStereoResult(
            points_left=points,
            valid_pixels=int(valid.sum()),
            median_depth_m=(
                float(np.median(valid_depth)) if len(valid_depth) else None
            ),
            median_disparity_px=(
                float(np.median(valid_disparity)) if len(valid_disparity) else None
            ),
        )


class DenseVoxelMap:
    """把稠密世界点按体素运行均值融合，并限制总内存。"""

    def __init__(self, *, voxel_size_m: float = 0.04, max_voxels: int = 300_000) -> None:
        if voxel_size_m <= 0.0 or max_voxels <= 0:
            raise ValueError("voxel_size_m and max_voxels must be positive")
        self.voxel_size_m = float(voxel_size_m)
        self.max_voxels = int(max_voxels)
        self._positions: Dict[VoxelKey, np.ndarray] = {}
        self._counts: Dict[VoxelKey, int] = {}

    def clear(self) -> None:
        self._positions.clear()
        self._counts.clear()

    def update(self, points_world: np.ndarray) -> int:
        """Fuse at most one point from this frame into each occupied voxel."""

        points = np.asarray(points_world, dtype=np.float32).reshape(-1, 3)
        points = points[np.isfinite(points).all(axis=1)]
        if not len(points):
            return 0
        keys = np.floor(points / self.voxel_size_m).astype(np.int32)
        # 同一帧每体素最多贡献一个样本，避免近距离密集像素支配运行均值。
        _, representative_indices = np.unique(keys, axis=0, return_index=True)
        added = 0
        for index in representative_indices:
            key = tuple(int(value) for value in keys[index])
            point = points[index]
            previous = self._positions.get(key)
            if previous is None:
                if len(self._positions) >= self.max_voxels:
                    continue
                self._positions[key] = point.copy()
                self._counts[key] = 1
                added += 1
                continue
            count = self._counts[key] + 1
            previous += (point - previous) / count
            self._counts[key] = count
        return added

    def points(self) -> np.ndarray:
        if not self._positions:
            return np.empty((0, 3), dtype=np.float32)
        return np.asarray(list(self._positions.values()), dtype=np.float32)

    def __len__(self) -> int:
        return len(self._positions)
