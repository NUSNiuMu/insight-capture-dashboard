#!/usr/bin/env python3

"""通过本地 Unix socket 串行提供 SuperPoint/SuperGlue TensorRT 推理。

该进程独占 TensorRT context 与 CUDA stream，mapper 和两路 localizer 通过短连接
共享它。服务端一次只处理一个请求，避免多个进程并发操作同一执行上下文和显存缓冲。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import time
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from insight_capture.runtime.mapping.superglue_ipc import receive_message, send_message
except ModuleNotFoundError:  # isolated SuperGlue runtime image
    from superglue_ipc import receive_message, send_message


class InferenceServer:
    """无状态请求协议的单线程 Unix socket 服务。"""

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
        """创建 socket、串行接收请求，并在退出时清理 socket 文件。"""

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
            f"cuda={self.matcher.cuda_version}, "
            f"device={self.matcher.device_name}",
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
                # TensorRT context 不是跨请求并行使用；连接在当前线程完整处理后关闭。
                with connection:
                    connection.settimeout(30.0)
                    self._serve_one(connection)
        finally:
            self._server = None
            server.close()
            if self.socket_path.is_socket():
                self.socket_path.unlink()

    def _serve_one(self, connection: socket.socket) -> None:
        """验证一条请求，执行 health/extract/match 之一并返回裸数组。"""

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
                        "cuda": self.matcher.cuda_version,
                        "device": self.matcher.device_name,
                        "compute_capability": list(
                            self.matcher.compute_capability
                        ),
                    },
                )
                return
            if command == "extract":
                # 单图提取供全局描述子地图回退路径使用。
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
            # 双图匹配同时服务 Insight9 双目和 Insight3→关键帧直接定位。
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
    parser.add_argument("--keypoint-threshold", type=float, default=0.005)
    parser.add_argument("--engine-dir", default="/opt/insight/engines")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        from insight_capture.runtime.mapping.superglue_tensorrt_runtime import TensorRTMatcher
    except ModuleNotFoundError:
        from superglue_tensorrt_runtime import TensorRTMatcher

    matcher = TensorRTMatcher(
        Path(args.engine_dir),
        keypoint_threshold=args.keypoint_threshold,
    )
    matcher.warmup()
    server = InferenceServer(Path(args.socket), matcher)
    signal.signal(signal.SIGTERM, server.request_stop)
    signal.signal(signal.SIGINT, server.request_stop)
    try:
        server.run()
    finally:
        matcher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
