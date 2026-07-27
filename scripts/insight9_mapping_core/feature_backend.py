"""Feature-matcher interface and adapter for Magic Leap's official models."""

from __future__ import annotations

import importlib
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from superglue_ipc import receive_message, send_message


OFFICIAL_SUPERGLUE_COMMIT = "ddcf11f42e7e0732a0c4607648f9448ea8d73590"


@dataclass(frozen=True)
class StereoMatches:
    """Matched keypoints and left-image descriptors."""

    left_points: np.ndarray
    right_points: np.ndarray
    descriptors: np.ndarray
    scores: np.ndarray
    detected_left: int
    detected_right: int
    backend_inference_ms: Optional[float] = None


@dataclass(frozen=True)
class ImageFeatures:
    """SuperPoint features extracted from one grayscale image."""

    keypoints: np.ndarray
    descriptors: np.ndarray
    scores: np.ndarray
    backend_inference_ms: Optional[float] = None


class OfficialSuperGlueBackend:
    """Run the pinned official SuperPoint + SuperGlue PyTorch implementation."""

    def __init__(
        self,
        checkout: Path,
        *,
        weights: str = "indoor",
        max_keypoints: int = 1024,
        keypoint_threshold: float = 0.005,
        match_threshold: float = 0.2,
        device: str = "cuda",
    ) -> None:
        checkout = Path(checkout).resolve()
        matching_file = checkout / "models" / "matching.py"
        weights_dir = checkout / "models" / "weights"
        required_weights = (
            weights_dir / "superpoint_v1.pth",
            weights_dir / f"superglue_{weights}.pth",
        )
        if not matching_file.is_file() or any(not path.is_file() for path in required_weights):
            raise RuntimeError(
                "official SuperGlue checkout is incomplete; run "
                "scripts/setup_superglue_validation.sh --accept-license"
            )
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError(
                "PyTorch is absent. Use an NVIDIA Jetson PyTorch container for the "
                "official GPU baseline; do not install an arbitrary ARM wheel."
            ) from exc
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but the PyTorch build has no CUDA device")
        if device not in {"cuda", "cpu"}:
            raise ValueError("device must be cuda or cpu")

        sys.path.insert(0, str(checkout))
        try:
            matching_module = importlib.import_module("models.matching")
        finally:
            try:
                sys.path.remove(str(checkout))
            except ValueError:
                pass
        config = {
            "superpoint": {
                "nms_radius": 4,
                "keypoint_threshold": float(keypoint_threshold),
                "max_keypoints": int(max_keypoints),
            },
            "superglue": {
                "weights": weights,
                "sinkhorn_iterations": 20,
                "match_threshold": float(match_threshold),
            },
        }
        self._torch = torch
        self._device = device
        self._matching = matching_module.Matching(config).eval().to(device)

    def extract(self, gray: np.ndarray) -> ImageFeatures:
        """Extract SuperPoint features from one uint8 grayscale image."""

        image = np.asarray(gray)
        if image.dtype != np.uint8 or image.ndim != 2:
            raise ValueError("extractor input must be a uint8 grayscale image")
        torch = self._torch
        tensor = torch.from_numpy(image).to(self._device, non_blocking=True)
        tensor = tensor.float().div_(255.0)[None, None]
        with torch.inference_mode():
            prediction = self._matching.superpoint({"image": tensor})
        return ImageFeatures(
            keypoints=prediction["keypoints"][0].detach().cpu().numpy().astype(
                np.float32
            ),
            descriptors=prediction["descriptors"][0]
            .detach()
            .cpu()
            .numpy()
            .T.astype(np.float32),
            scores=prediction["scores"][0].detach().cpu().numpy().astype(np.float32),
        )

    def match(
        self,
        left_gray: np.ndarray,
        right_gray: np.ndarray,
        *,
        left_mask: Optional[np.ndarray] = None,
    ) -> StereoMatches:
        """Extract and match features from two same-sized uint8 grayscale images."""

        left = np.asarray(left_gray)
        right = np.asarray(right_gray)
        if left.dtype != np.uint8 or right.dtype != np.uint8 or left.shape != right.shape:
            raise ValueError("matcher inputs must be same-sized uint8 grayscale images")
        torch = self._torch
        image0 = torch.from_numpy(left).to(self._device, non_blocking=True)
        image1 = torch.from_numpy(right).to(self._device, non_blocking=True)
        image0 = image0.float().div_(255.0)[None, None]
        image1 = image1.float().div_(255.0)[None, None]
        with torch.inference_mode():
            prediction = self._matching({"image0": image0, "image1": image1})

        keypoints0 = prediction["keypoints0"][0].detach().cpu().numpy()
        keypoints1 = prediction["keypoints1"][0].detach().cpu().numpy()
        descriptors0 = prediction["descriptors0"][0].detach().cpu().numpy().T
        matches0 = prediction["matches0"][0].detach().cpu().numpy()
        scores0 = prediction["matching_scores0"][0].detach().cpu().numpy()
        valid = matches0 >= 0
        if left_mask is not None and len(keypoints0):
            mask = np.asarray(left_mask, dtype=bool)
            pixels = np.rint(keypoints0).astype(np.int64)
            inside = (
                (pixels[:, 0] >= 0)
                & (pixels[:, 0] < mask.shape[1])
                & (pixels[:, 1] >= 0)
                & (pixels[:, 1] < mask.shape[0])
            )
            masked = np.zeros((len(pixels),), dtype=bool)
            masked[inside] = mask[pixels[inside, 1], pixels[inside, 0]]
            valid &= ~masked
        left_indices = np.flatnonzero(valid)
        right_indices = matches0[left_indices].astype(np.int64)
        return StereoMatches(
            left_points=keypoints0[left_indices].astype(np.float32),
            right_points=keypoints1[right_indices].astype(np.float32),
            descriptors=descriptors0[left_indices].astype(np.float32),
            scores=scores0[left_indices].astype(np.float32),
            detected_left=len(keypoints0),
            detected_right=len(keypoints1),
        )


class IpcSuperGlueBackend:
    """Use the NVIDIA PyTorch validation worker through a local Unix socket."""

    def __init__(self, socket_path: Path, *, timeout_sec: float = 30.0) -> None:
        self._socket_path = Path(socket_path)
        self._timeout_sec = float(timeout_sec)
        health = self.health()
        if not health.get("ok"):
            raise RuntimeError(f"SuperGlue inference worker is unhealthy: {health}")

    def _request(self, metadata, payloads=()):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout_sec)
        try:
            connection.connect(str(self._socket_path))
            send_message(connection, metadata, payloads)
            return receive_message(connection)
        except OSError as exc:
            raise RuntimeError(
                f"cannot reach SuperGlue inference socket {self._socket_path}: {exc}"
            ) from exc
        finally:
            connection.close()

    def health(self):
        metadata, payloads = self._request({"command": "health"})
        if payloads:
            raise RuntimeError("invalid health response from SuperGlue worker")
        return metadata

    def extract(self, gray: np.ndarray) -> ImageFeatures:
        """Extract SuperPoint features through the inference worker."""

        image = np.ascontiguousarray(gray, dtype=np.uint8)
        if image.ndim != 2:
            raise ValueError("extractor input must be a grayscale image")
        metadata, payloads = self._request(
            {"command": "extract", "height": image.shape[0], "width": image.shape[1]},
            (image.tobytes(),),
        )
        if not metadata.get("ok"):
            raise RuntimeError(
                f"SuperPoint worker failed: {metadata.get('error', 'unknown')}"
            )
        if len(payloads) != 3:
            raise RuntimeError("invalid extraction response from SuperPoint worker")
        count = int(metadata["keypoints"])
        descriptor_dim = int(metadata["descriptor_dim"])
        expected_sizes = (
            count * 2 * 4,
            count * descriptor_dim * 4,
            count * 4,
        )
        if tuple(len(payload) for payload in payloads) != expected_sizes:
            raise RuntimeError("SuperPoint worker returned inconsistent array sizes")
        return ImageFeatures(
            keypoints=np.frombuffer(payloads[0], dtype=np.float32)
            .reshape(count, 2)
            .copy(),
            descriptors=np.frombuffer(payloads[1], dtype=np.float32)
            .reshape(count, descriptor_dim)
            .copy(),
            scores=np.frombuffer(payloads[2], dtype=np.float32).copy(),
            backend_inference_ms=float(metadata["inference_ms"]),
        )

    def match(
        self,
        left_gray: np.ndarray,
        right_gray: np.ndarray,
        *,
        left_mask: Optional[np.ndarray] = None,
    ) -> StereoMatches:
        left = np.ascontiguousarray(left_gray, dtype=np.uint8)
        right = np.ascontiguousarray(right_gray, dtype=np.uint8)
        if left.ndim != 2 or right.shape != left.shape:
            raise ValueError("matcher inputs must be same-sized grayscale images")
        metadata, payloads = self._request(
            {"command": "match", "height": left.shape[0], "width": left.shape[1]},
            (left.tobytes(), right.tobytes()),
        )
        if not metadata.get("ok"):
            raise RuntimeError(f"SuperGlue worker failed: {metadata.get('error', 'unknown')}")
        if len(payloads) != 4:
            raise RuntimeError("invalid match response from SuperGlue worker")
        count = int(metadata["matches"])
        descriptor_dim = int(metadata["descriptor_dim"])
        expected_sizes = (
            count * 2 * 4,
            count * 2 * 4,
            count * descriptor_dim * 4,
            count * 4,
        )
        if tuple(len(payload) for payload in payloads) != expected_sizes:
            raise RuntimeError("SuperGlue worker returned inconsistent array sizes")
        left_points = np.frombuffer(payloads[0], dtype=np.float32).reshape(count, 2).copy()
        right_points = np.frombuffer(payloads[1], dtype=np.float32).reshape(count, 2).copy()
        descriptors = (
            np.frombuffer(payloads[2], dtype=np.float32)
            .reshape(count, descriptor_dim)
            .copy()
        )
        scores = np.frombuffer(payloads[3], dtype=np.float32).copy()
        if left_mask is not None and count:
            mask = np.asarray(left_mask, dtype=bool)
            pixels = np.rint(left_points).astype(np.int64)
            inside = (
                (pixels[:, 0] >= 0)
                & (pixels[:, 0] < mask.shape[1])
                & (pixels[:, 1] >= 0)
                & (pixels[:, 1] < mask.shape[0])
            )
            keep = np.ones((count,), dtype=bool)
            keep[inside] = ~mask[pixels[inside, 1], pixels[inside, 0]]
            left_points = left_points[keep]
            right_points = right_points[keep]
            descriptors = descriptors[keep]
            scores = scores[keep]
        return StereoMatches(
            left_points=left_points,
            right_points=right_points,
            descriptors=descriptors,
            scores=scores,
            detected_left=int(metadata["detected_left"]),
            detected_right=int(metadata["detected_right"]),
            backend_inference_ms=float(metadata["inference_ms"]),
        )
