"""Local WiLoR-mini implementation of the hand-pose backend contract."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
from typing import Sequence

import numpy as np
from scipy.spatial.transform import Rotation

from .model import HandPosePrediction


DEFAULT_MODEL_DIR = Path("/opt/insight/models/wilor")
MODEL_FILES = (
    "wilor_final.ckpt",
    "detector.pt",
    "MANO_RIGHT.pkl",
    "mano_mean_params.npz",
)


class WiLoRBackend:
    """Run the bundled detector and WiLoR regressor without changing handedness."""

    name = "wilor"

    def __init__(
        self,
        *,
        model_dir: Path | None,
        confidence: float,
        focal_length: float,
    ) -> None:
        import torch
        from wilor_mini.pipelines.wilor_hand_pose3d_estimation_pipeline import (
            WiLorHandPose3dEstimationPipeline,
        )

        configured = os.environ.get("HANDPOSE_WILOR_MODEL_DIR", "").strip()
        self.model_dir = Path(
            model_dir or (Path(configured) if configured else DEFAULT_MODEL_DIR)
        ).resolve()
        pretrained = self.model_dir / "pretrained_models"
        missing = [name for name in MODEL_FILES if not (pretrained / name).is_file()]
        if missing:
            raise FileNotFoundError(f"missing WiLoR model files: {', '.join(missing)}")
        if not 0.0 < confidence <= 1.0:
            raise ValueError("hand confidence must be within (0, 1]")
        self.confidence = float(confidence)
        self._torch = torch
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float16 if device.type == "cuda" else torch.float32
        self._pipeline = WiLorHandPose3dEstimationPipeline(
            device=device,
            dtype=dtype,
            focal_length=float(focal_length),
            wilor_pretrained_dir=str(self.model_dir),
            verbose=False,
        )
        try:
            self.version = importlib.metadata.version("wilor-mini")
        except importlib.metadata.PackageNotFoundError:
            self.version = "unknown"

    def cache_identity(self) -> dict[str, object]:
        pretrained = self.model_dir / "pretrained_models"
        return {
            "backend": self.name,
            "version": self.version,
            "confidence": self.confidence,
            "model_files": {
                name: {
                    "size": (pretrained / name).stat().st_size,
                    "mtime_ns": (pretrained / name).stat().st_mtime_ns,
                }
                for name in MODEL_FILES
            },
        }

    def predict(self, image_bgr: np.ndarray) -> Sequence[HandPosePrediction]:
        predictions = []
        with self._torch.inference_mode():
            detections = self._pipeline.hand_detector(
                image_bgr, conf=self.confidence, verbose=False
            )[0]
            boxes, labels, scores = [], [], []
            for detection in detections:
                values = detection.boxes.data.cpu().detach().reshape(-1).numpy()
                boxes.append(values[:4])
                labels.append(float(detection.boxes.cls.cpu().detach().reshape(-1)[0]))
                scores.append(float(detection.boxes.conf.cpu().detach().reshape(-1)[0]))
            if not boxes:
                return predictions
            outputs = self._pipeline.predict_with_bboxes(
                image_bgr,
                np.asarray(boxes, dtype=np.float32),
                np.asarray(labels, dtype=np.float32),
            )
        for output, score in zip(outputs, scores):
            raw = output.get("wilor_preds")
            if raw is None:
                continue
            keypoints_camera = (
                np.asarray(raw["pred_keypoints_3d"], dtype=np.float64)[0]
                + np.asarray(raw["pred_cam_t_full"], dtype=np.float64)[0][None, :]
            )
            wrist_rotation = Rotation.from_rotvec(
                np.asarray(raw["global_orient"], dtype=np.float64)[0, 0]
            ).as_quat()
            predictions.append(
                HandPosePrediction(
                    handedness="right" if bool(output["is_right"]) else "left",
                    confidence=score,
                    bbox_xyxy=np.asarray(output["hand_bbox"], dtype=np.float64).reshape(4),
                    keypoints_2d=np.asarray(raw["pred_keypoints_2d"], dtype=np.float64)[0],
                    keypoints_3d_camera=keypoints_camera,
                    wrist_rotation_camera_xyzw=wrist_rotation,
                    mano_pose_axis_angle=np.asarray(raw["hand_pose"], dtype=np.float64)[0].reshape(45),
                )
            )
        return predictions

    def close(self) -> None:
        self._pipeline = None
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()
