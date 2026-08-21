"""根据重定位修正幅度选择“立即跳转”或“EKF 平滑吸收”。

PnP 给出的 ``T_map_odom`` 是低频绝对观测。小修正通常来自正常累计漂移，直接跳转
会让网页轨迹和机器人末端位姿抖动；大修正则意味着坐标已经明显错误，继续缓慢融合
会在较长时间内输出错误世界坐标。本模块只负责策略判断，滤波状态仍由
``RelocalizationEkf`` 管理。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import rotation_distance_deg
from .relocalization_ekf import RelocalizationEkf


@dataclass(frozen=True)
class AdaptiveRelocalizationConfig:
    """超过任一阈值时立即采用已确认的全局修正。"""

    jump_translation_m: float = 0.15
    jump_rotation_deg: float = 10.0

    def __post_init__(self) -> None:
        if (
            not np.isfinite(self.jump_translation_m)
            or self.jump_translation_m <= 0.0
            or not np.isfinite(self.jump_rotation_deg)
            or self.jump_rotation_deg <= 0.0
        ):
            raise ValueError("adaptive relocalization thresholds must be positive")


@dataclass(frozen=True)
class AdaptiveRelocalizationUpdate:
    """一次绝对修正的应用结果，供状态诊断显示修正模式和幅度。"""

    mode: str
    translation_m: float
    rotation_deg: float


class AdaptiveRelocalizationPolicy:
    """严重漂移时立即重置，小幅漂移时保留 EKF 平滑。"""

    def __init__(self, config: AdaptiveRelocalizationConfig) -> None:
        self.config = config

    def apply(
        self, pose_filter: RelocalizationEkf, measurement: np.ndarray
    ) -> AdaptiveRelocalizationUpdate:
        value = np.asarray(measurement, dtype=np.float64).reshape(4, 4)
        current = pose_filter.correction
        if current is None:
            # 第一次全局定位没有可平滑过渡的旧世界系，必须直接建立坐标关系。
            pose_filter.observe(value)
            return AdaptiveRelocalizationUpdate("initialize", 0.0, 0.0)

        translation_m = float(np.linalg.norm(value[:3, 3] - current[:3, 3]))
        rotation_deg = rotation_distance_deg(current, value)
        if (
            translation_m >= self.config.jump_translation_m
            or rotation_deg >= self.config.jump_rotation_deg
        ):
            # 大修正继续走普通 Kalman 增益会长期保留旧误差，因此明确硬切换。
            pose_filter.reinitialize(value)
            mode = "jump"
        else:
            pose_filter.observe(value)
            mode = "ekf"
        return AdaptiveRelocalizationUpdate(mode, translation_m, rotation_deg)
