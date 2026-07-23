"""Online alignment sample state models."""

from dataclasses import dataclass

import numpy as np

@dataclass
class DetectionSample:
    stamp_ns: int
    marker_transform: np.ndarray
