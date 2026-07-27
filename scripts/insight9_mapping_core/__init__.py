"""Realtime sparse stereo mapping primitives for Insight9."""

from .geometry import (
    PoseSample,
    StereoCalibration,
    compose_transform,
    interpolate_pose,
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
)
from .synchronization import PoseBuffer, StereoPair, StereoPairSynchronizer

__all__ = [
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
    "StereoMatches",
    "StereoPair",
    "StereoCalibration",
    "StereoPairSynchronizer",
    "compose_transform",
    "interpolate_pose",
    "matrix_from_pose",
    "matrix_from_transform",
    "localize_features",
    "match_descriptors",
    "rotation_distance_deg",
    "transform_points",
    "triangulate_rectified",
]
