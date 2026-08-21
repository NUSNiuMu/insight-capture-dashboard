"""Insight9 实时稀疏建图与 Insight3 全局重定位的公共接口。

生产系统由三个进程组成：SuperPoint/SuperGlue 推理服务、Insight9 稀疏建图器和
Insight3 全局定位器。本模块只负责集中导出它们共享的数据结构与算法，不持有 ROS
状态，也不会自行启动线程或订阅话题。阅读主流程时应从 ``insight9_mapper`` 和
``insight3_localizer`` 开始，再根据这里的导入关系进入具体算法模块。
"""

from .adaptive_relocalization import (
    AdaptiveRelocalizationConfig,
    AdaptiveRelocalizationPolicy,
    AdaptiveRelocalizationUpdate,
)
from .geometry import (
    PoseSample,
    StereoCalibration,
    compose_transform,
    interpolate_pose,
    left_to_stereo_center,
    matrix_from_pose,
    matrix_from_transform,
    rotation_distance_deg,
    transform_points,
    triangulate_rectified,
)
from .dense_stereo import (
    DenseStereoConfig,
    DenseStereoEstimator,
    DenseStereoResult,
    DenseVoxelMap,
)
from .superglue_backend import (
    ImageFeatures,
    IpcSuperGlueBackend,
    OFFICIAL_SUPERGLUE_COMMIT,
    OfficialSuperGlueBackend,
    StereoMatches,
)
from .map_store import LandmarkMap, LandmarkMapConfig
from .global_localization import (
    GlobalLocalizationConfig,
    LocalizationCandidate,
    LocalizationConsensus,
    associate_reference_points,
    localize_correspondences,
    localize_features,
    match_descriptors,
    normalize_descriptors,
)
from .synchronization import (
    PoseBuffer,
    StereoPair,
    StereoPairSynchronizer,
    select_timestamp,
)
from .relocalization_ekf import RelocalizationEkf, RelocalizationEkfConfig
from .tcp_frames import TcpFrameCalibration, load_tcp_frame_calibrations
from .vio_continuity import VioContinuityConfig, VioContinuityStitcher

__all__ = [
    "AdaptiveRelocalizationConfig",
    "AdaptiveRelocalizationPolicy",
    "AdaptiveRelocalizationUpdate",
    "LandmarkMap",
    "LandmarkMapConfig",
    "GlobalLocalizationConfig",
    "LocalizationCandidate",
    "LocalizationConsensus",
    "DenseStereoConfig",
    "DenseStereoEstimator",
    "DenseStereoResult",
    "DenseVoxelMap",
    "IpcSuperGlueBackend",
    "ImageFeatures",
    "OFFICIAL_SUPERGLUE_COMMIT",
    "OfficialSuperGlueBackend",
    "PoseBuffer",
    "PoseSample",
    "RelocalizationEkf",
    "RelocalizationEkfConfig",
    "StereoMatches",
    "StereoPair",
    "StereoCalibration",
    "StereoPairSynchronizer",
    "TcpFrameCalibration",
    "VioContinuityConfig",
    "VioContinuityStitcher",
    "compose_transform",
    "associate_reference_points",
    "interpolate_pose",
    "left_to_stereo_center",
    "load_tcp_frame_calibrations",
    "matrix_from_pose",
    "matrix_from_transform",
    "localize_features",
    "localize_correspondences",
    "match_descriptors",
    "normalize_descriptors",
    "rotation_distance_deg",
    "select_timestamp",
    "transform_points",
    "triangulate_rectified",
]
