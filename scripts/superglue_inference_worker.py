#!/usr/bin/env python3

"""Serve Magic Leap's official SuperPoint/SuperGlue over a local Unix socket."""

from __future__ import annotations

import argparse
import importlib
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import torch

from superglue_ipc import receive_message, send_message


OFFICIAL_COMMIT = "ddcf11f42e7e0732a0c4607648f9448ea8d73590"


class OfficialMatcher:
    """Load and run the pinned official model in NVIDIA's Jetson container."""

    def __init__(
        self,
        checkout: Path,
        *,
        weights: str,
        max_keypoints: int,
        keypoint_threshold: float,
        match_threshold: float,
    ) -> None:
        if not torch.cuda.is_available():
            raise RuntimeError("NVIDIA PyTorch container cannot access the Jetson CUDA device")
        checkout = checkout.resolve()
        sys.path.insert(0, str(checkout))
        try:
            module = importlib.import_module("models.matching")
        finally:
            sys.path.remove(str(checkout))
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
        self._matching = module.Matching(config).eval().cuda()
        torch.backends.cudnn.benchmark = True
        self.backend_name = "pytorch"
        self.runtime_version = torch.__version__

    @staticmethod
    def _image_tensor(image: np.ndarray):
        return torch.from_numpy(image).cuda(non_blocking=True).float().div_(255.0)[
            None, None
        ]

    def extract(self, image: np.ndarray):
        """Extract SuperPoint keypoints, descriptors, and scores."""

        with torch.inference_mode():
            prediction = self._matching.superpoint({"image": self._image_tensor(image)})
        keypoints = prediction["keypoints"][0].detach().cpu().numpy()
        descriptors = prediction["descriptors"][0].detach().cpu().numpy().T
        scores = prediction["scores"][0].detach().cpu().numpy()
        return (
            keypoints.astype(np.float32),
            descriptors.astype(np.float32),
            scores.astype(np.float32),
        )

    def warmup(self, height: int = 640, width: int = 544, runs: int = 2) -> None:
        """Prime CUDA kernels and cuDNN selection before advertising health."""

        yy, xx = np.indices((height, width))
        left = (((xx // 32 + yy // 32) % 2) * 180 + 35).astype(np.uint8)
        right = np.roll(left, -8, axis=1).copy()
        for index in range(max(1, int(runs))):
            started = time.perf_counter()
            result = self.match(left, right)
            print(
                f"SuperGlue warmup {index + 1}/{runs}: "
                f"matches={len(result[0])}, "
                f"elapsed_ms={(time.perf_counter() - started) * 1000.0:.2f}",
                flush=True,
            )

    def match(self, left: np.ndarray, right: np.ndarray):
        image0 = self._image_tensor(left)
        image1 = self._image_tensor(right)
        with torch.inference_mode():
            prediction = self._matching({"image0": image0, "image1": image1})
        keypoints0 = prediction["keypoints0"][0].detach().cpu().numpy()
        keypoints1 = prediction["keypoints1"][0].detach().cpu().numpy()
        descriptors0 = prediction["descriptors0"][0].detach().cpu().numpy().T
        matches0 = prediction["matches0"][0].detach().cpu().numpy()
        scores0 = prediction["matching_scores0"][0].detach().cpu().numpy()
        left_indices = np.flatnonzero(matches0 >= 0)
        right_indices = matches0[left_indices].astype(np.int64)
        return (
            keypoints0[left_indices].astype(np.float32),
            keypoints1[right_indices].astype(np.float32),
            descriptors0[left_indices].astype(np.float32),
            scores0[left_indices].astype(np.float32),
            len(keypoints0),
            len(keypoints1),
        )


class InferenceServer:
    def __init__(self, socket_path: Path, matcher) -> None:
        self.socket_path = socket_path
        self.matcher = matcher
        self._stop = False
        self._server: Optional[socket.socket] = None

    def request_stop(self, *_args) -> None:
        self._stop = True
        if self._server is not None:
            self._server.close()

    def run(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            if not self.socket_path.is_socket():
                raise RuntimeError(f"refusing to replace non-socket path {self.socket_path}")
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o666)
        server.listen(4)
        server.settimeout(1.0)
        print(
            f"SuperGlue inference ready: socket={self.socket_path}, "
            f"backend={self.matcher.backend_name}, "
            f"runtime={self.matcher.runtime_version}, "
            f"torch={torch.__version__}, cuda={torch.version.cuda}",
            flush=True,
        )
        try:
            while not self._stop:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop:
                        break
                    raise
                with connection:
                    connection.settimeout(30.0)
                    self._serve_one(connection)
        finally:
            self._server = None
            server.close()
            if self.socket_path.is_socket():
                self.socket_path.unlink()

    def _serve_one(self, connection: socket.socket) -> None:
        try:
            metadata, payloads = receive_message(connection)
            command = metadata.get("command")
            if command == "health":
                send_message(
                    connection,
                    {
                        "ok": True,
                        "backend": self.matcher.backend_name,
                        "runtime": self.matcher.runtime_version,
                        "torch": torch.__version__,
                        "cuda": torch.version.cuda,
                        "device": torch.cuda.get_device_name(0),
                    },
                )
                return
            if command == "extract":
                if len(payloads) != 1:
                    raise ValueError("expected one grayscale image for extraction")
                height = int(metadata["height"])
                width = int(metadata["width"])
                expected = height * width
                if height <= 0 or width <= 0 or len(payloads[0]) != expected:
                    raise ValueError("invalid grayscale image dimensions")
                image = (
                    np.frombuffer(payloads[0], dtype=np.uint8)
                    .reshape(height, width)
                    .copy()
                )
                started = time.perf_counter()
                keypoints, descriptors, scores = self.matcher.extract(image)
                send_message(
                    connection,
                    {
                        "ok": True,
                        "keypoints": len(keypoints),
                        "descriptor_dim": (
                            descriptors.shape[1] if descriptors.ndim == 2 else 0
                        ),
                        "inference_ms": round(
                            (time.perf_counter() - started) * 1000.0, 2
                        ),
                    },
                    (
                        keypoints.tobytes(),
                        descriptors.tobytes(),
                        scores.tobytes(),
                    ),
                )
                return
            if command != "match" or len(payloads) != 2:
                raise ValueError("expected a match request with two grayscale images")
            height = int(metadata["height"])
            width = int(metadata["width"])
            expected = height * width
            if height <= 0 or width <= 0 or any(len(value) != expected for value in payloads):
                raise ValueError("invalid grayscale image dimensions")
            left = (
                np.frombuffer(payloads[0], dtype=np.uint8)
                .reshape(height, width)
                .copy()
            )
            right = (
                np.frombuffer(payloads[1], dtype=np.uint8)
                .reshape(height, width)
                .copy()
            )
            started = time.perf_counter()
            left_points, right_points, descriptors, scores, detected_left, detected_right = (
                self.matcher.match(left, right)
            )
            send_message(
                connection,
                {
                    "ok": True,
                    "matches": len(left_points),
                    "descriptor_dim": descriptors.shape[1] if descriptors.ndim == 2 else 0,
                    "detected_left": detected_left,
                    "detected_right": detected_right,
                    "inference_ms": round((time.perf_counter() - started) * 1000.0, 2),
                },
                (
                    left_points.tobytes(),
                    right_points.tobytes(),
                    descriptors.tobytes(),
                    scores.tobytes(),
                ),
            )
        except Exception as exc:
            try:
                send_message(connection, {"ok": False, "error": str(exc)})
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/superglue/matcher.sock")
    parser.add_argument(
        "--checkout", default="/opt/SuperGluePretrainedNetwork"
    )
    parser.add_argument("--weights", choices=("indoor", "outdoor"), default="indoor")
    parser.add_argument("--max-keypoints", type=int, default=1024)
    parser.add_argument("--keypoint-threshold", type=float, default=0.005)
    parser.add_argument("--match-threshold", type=float, default=0.2)
    parser.add_argument(
        "--backend", choices=("tensorrt", "pytorch"), default="tensorrt"
    )
    parser.add_argument("--engine-dir", default="/opt/insight/engines")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.backend == "tensorrt":
        from superglue_tensorrt import TensorRTMatcher

        matcher = TensorRTMatcher(
            Path(args.checkout),
            Path(args.engine_dir),
            max_keypoints=args.max_keypoints,
            keypoint_threshold=args.keypoint_threshold,
        )
    else:
        matcher = OfficialMatcher(
            Path(args.checkout),
            weights=args.weights,
            max_keypoints=args.max_keypoints,
            keypoint_threshold=args.keypoint_threshold,
            match_threshold=args.match_threshold,
        )
    matcher.warmup()
    server = InferenceServer(Path(args.socket), matcher)
    signal.signal(signal.SIGTERM, server.request_stop)
    signal.signal(signal.SIGINT, server.request_stop)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
