"""Realtime sparse stereo mapping primitives for Insight9."""

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
from .feature_backend import (
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
    "interpolate_pose",
    "left_to_stereo_center",
    "load_tcp_frame_calibrations",
    "matrix_from_pose",
    "matrix_from_transform",
    "localize_features",
    "match_descriptors",
    "normalize_descriptors",
    "rotation_distance_deg",
    "select_timestamp",
    "transform_points",
    "triangulate_rectified",
]
