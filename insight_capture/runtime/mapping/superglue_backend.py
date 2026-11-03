"""SuperPoint/SuperGlue data structures and the production TensorRT IPC client."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from insight_capture.runtime.mapping.superglue_ipc import receive_message, send_message




@dataclass(frozen=True)
class StereoMatches:
    """左右匹配坐标、左图描述子、匹配分数及推理诊断。"""

    left_points: np.ndarray
    right_points: np.ndarray
    descriptors: np.ndarray
    scores: np.ndarray
    detected_left: int
    detected_right: int
    backend_inference_ms: Optional[float] = None


@dataclass(frozen=True)
class ImageFeatures:
    """单张灰度图提取的 SuperPoint 坐标、描述子和检测分数。"""

    keypoints: np.ndarray
    descriptors: np.ndarray
    scores: np.ndarray
    backend_inference_ms: Optional[float] = None


class IpcSuperGlueBackend:
    """通过本地 Unix socket 使用隔离的 TensorRT 推理 worker。"""

    def __init__(self, socket_path: Path, *, timeout_sec: float = 30.0) -> None:
        self._socket_path = Path(socket_path)
        self._timeout_sec = float(timeout_sec)
        health = self.health()
        if not health.get("ok"):
            raise RuntimeError(f"SuperGlue inference worker is unhealthy: {health}")

    def _request(self, metadata, payloads=()):
        # 每个请求单独建短连接；worker 串行处理，协议无需连接级会话状态。
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
        # IPC 中的 ndarray 没有自描述头，必须用元数据严格校验字节数后再 reshape。
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
            # mask 在客户端执行，避免让通用推理服务耦合具体相机的夹爪区域配置。
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
