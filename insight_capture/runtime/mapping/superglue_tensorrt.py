"""镜像构建阶段使用的固定官方模型 ONNX 导出代码。

生产容器运行时不导入本文件，也不携带 PyTorch。前半部分把官方 SuperPoint 的
后处理一起封装进固定输出 ONNX，并把 SuperGlue 导出为动态点数图；后半部分保留的
PyTorch-CUDA TensorRT 执行器仅用于早期开发对照，当前生产调用
``superglue_tensorrt_runtime.py`` 中的纯 NumPy/CUDA 实现。
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F


IMAGE_HEIGHT = 640
IMAGE_WIDTH = 544
DESCRIPTOR_DIM = 256
OFFICIAL_COMMIT = "ddcf11f42e7e0732a0c4607648f9448ea8d73590"


class SuperPointEndToEnd(nn.Module):
    """把 SuperPoint 主干和后处理导出成固定数量输出，运行时无需 Torch。"""

    def __init__(
        self,
        superpoint: nn.Module,
        *,
        max_keypoints: int,
        keypoint_threshold: float,
    ) -> None:
        super().__init__()
        self.max_keypoints = int(max_keypoints)
        self.keypoint_threshold = float(keypoint_threshold)
        self.relu = superpoint.relu
        self.pool = superpoint.pool
        for name in (
            "conv1a",
            "conv1b",
            "conv2a",
            "conv2b",
            "conv3a",
            "conv3b",
            "conv4a",
            "conv4b",
            "convPa",
            "convPb",
            "convDa",
            "convDb",
        ):
            setattr(self, name, getattr(superpoint, name))
        border_mask = torch.zeros(1, IMAGE_HEIGHT, IMAGE_WIDTH)
        border_mask[:, 4:-4, 4:-4] = 1.0
        self.register_buffer("border_mask", border_mask, persistent=False)

    @staticmethod
    def _simple_nms(scores: torch.Tensor, radius: int = 4) -> torch.Tensor:
        """复现官方两轮 max-pool NMS，并保持可导出为 ONNX 算子。"""

        def max_pool(value: torch.Tensor) -> torch.Tensor:
            return F.max_pool2d(
                value, kernel_size=radius * 2 + 1, stride=1, padding=radius
            )

        zeros = torch.zeros_like(scores)
        max_mask = scores == max_pool(scores)
        for _ in range(2):
            suppression_mask = max_pool(max_mask.float()) > 0
            suppressed_scores = torch.where(suppression_mask, zeros, scores)
            new_max_mask = suppressed_scores == max_pool(suppressed_scores)
            max_mask = max_mask | (new_max_mask & (~suppression_mask))
        return torch.where(max_mask, scores, zeros)

    @staticmethod
    def _sample_descriptors(
        keypoints: torch.Tensor, descriptors: torch.Tensor, scale: int = 8
    ) -> torch.Tensor:
        """在 1/8 分辨率描述子图上按关键点位置双线性采样并归一化。"""

        batch, channels, height, width = descriptors.shape
        keypoints = keypoints - scale / 2 + 0.5
        normalizer = torch.tensor(
            [width * scale - scale / 2 - 0.5, height * scale - scale / 2 - 0.5],
            dtype=keypoints.dtype,
            device=keypoints.device,
        )
        keypoints = keypoints / normalizer[None] * 2 - 1
        sampled = F.grid_sample(
            descriptors,
            keypoints.view(batch, 1, -1, 2),
            mode="bilinear",
            align_corners=True,
        )
        sampled = F.normalize(sampled.reshape(batch, channels, -1), p=2, dim=1)
        return sampled

    def forward(
        self, image: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        features = self.relu(self.conv1a(image))
        features = self.relu(self.conv1b(features))
        features = self.pool(features)
        features = self.relu(self.conv2a(features))
        features = self.relu(self.conv2b(features))
        features = self.pool(features)
        features = self.relu(self.conv3a(features))
        features = self.relu(self.conv3b(features))
        features = self.pool(features)
        features = self.relu(self.conv4a(features))
        features = self.relu(self.conv4b(features))
        score_logits = self.convPb(self.relu(self.convPa(features)))
        dense_descriptors = self.convDb(self.relu(self.convDa(features)))

        # 65 类中的最后一类是 dustbin；其余 64 类重排回每个 8x8 网格的像素热图。
        scores = F.softmax(score_logits, dim=1)[:, :-1]
        batch, _, height, width = scores.shape
        scores = scores.permute(0, 2, 3, 1).reshape(
            batch, height, width, 8, 8
        )
        scores = scores.permute(0, 1, 3, 2, 4).reshape(
            batch, height * 8, width * 8
        )
        scores = self._simple_nms(scores[:, None])[:, 0] * self.border_mask
        top_scores, flat_indices = torch.topk(
            scores.flatten(1), self.max_keypoints, dim=1
        )
        # Top-K 热图索引直接换算成整像素 (x, y)；这里没有亚像素位置细化。
        keypoints = torch.stack(
            (
                torch.remainder(flat_indices, IMAGE_WIDTH),
                torch.div(flat_indices, IMAGE_WIDTH, rounding_mode="floor"),
            ),
            dim=2,
        ).float()
        dense_descriptors = F.normalize(dense_descriptors, p=2, dim=1)
        descriptors = self._sample_descriptors(keypoints, dense_descriptors)
        # 固定输出形状便于 TensorRT 建引擎；低于阈值的槽位置零，运行时再过滤。
        top_scores = torch.where(
            top_scores > self.keypoint_threshold,
            top_scores,
            torch.zeros_like(top_scores),
        )
        return keypoints[0], descriptors[0].transpose(0, 1), top_scores[0]


class SuperGlueCore(nn.Module):
    """把官方 SuperGlue 包装为纯张量输入输出，同时保留原始匹配计算。"""

    def __init__(self, superglue: nn.Module) -> None:
        super().__init__()
        self.superglue = superglue
        self.register_buffer(
            "image_shape_reference",
            torch.empty(1, 1, IMAGE_HEIGHT, IMAGE_WIDTH),
            persistent=False,
        )

    def forward(
        self,
        keypoints0: torch.Tensor,
        keypoints1: torch.Tensor,
        scores0: torch.Tensor,
        scores1: torch.Tensor,
        descriptors0: torch.Tensor,
        descriptors1: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        result = self.superglue(
            {
                "keypoints0": keypoints0,
                "keypoints1": keypoints1,
                "scores0": scores0,
                "scores1": scores1,
                "descriptors0": descriptors0,
                "descriptors1": descriptors1,
                "image0": self.image_shape_reference,
                "image1": self.image_shape_reference,
            }
        )
        return (
            result["matches0"],
            result["matches1"],
            result["matching_scores0"],
            result["matching_scores1"],
        )


def _load_matching(
    checkout: Path,
    *,
    weights: str,
    max_keypoints: int,
    keypoint_threshold: float,
    match_threshold: float,
    sinkhorn_iterations: int,
) -> nn.Module:
    checkout = checkout.resolve()
    sys.path.insert(0, str(checkout))
    try:
        module = importlib.import_module("models.matching")
    finally:
        sys.path.remove(str(checkout))
    return module.Matching(
        {
            "superpoint": {
                "nms_radius": 4,
                "keypoint_threshold": float(keypoint_threshold),
                "max_keypoints": int(max_keypoints),
            },
            "superglue": {
                "weights": weights,
                "sinkhorn_iterations": int(sinkhorn_iterations),
                "match_threshold": float(match_threshold),
            },
        }
    ).eval()


def export_onnx(
    checkout: Path,
    output_dir: Path,
    *,
    weights: str,
    max_keypoints: int,
    keypoint_threshold: float,
    match_threshold: float,
    sinkhorn_iterations: int,
) -> None:
    """导出固定输出 SuperPoint 与动态关键点数量的 SuperGlue 图。"""

    matching = _load_matching(
        checkout,
        weights=weights,
        max_keypoints=max_keypoints,
        keypoint_threshold=keypoint_threshold,
        match_threshold=match_threshold,
        sinkhorn_iterations=sinkhorn_iterations,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    superpoint_path = output_dir / "superpoint.onnx"
    superglue_path = output_dir / "superglue.onnx"

    superpoint = SuperPointEndToEnd(
        matching.superpoint,
        max_keypoints=max_keypoints,
        keypoint_threshold=keypoint_threshold,
    ).eval()
    torch.onnx.export(
        superpoint,
        (torch.rand(1, 1, IMAGE_HEIGHT, IMAGE_WIDTH),),
        superpoint_path,
        input_names=["image"],
        output_names=["keypoints", "descriptors", "scores"],
        opset_version=17,
        do_constant_folding=True,
    )

    # 示例点数只用于 tracing；dynamic_axes 允许最终 TensorRT profile 接受 1..1024 点。
    count0, count1 = 256, 320
    superglue = SuperGlueCore(matching.superglue).eval()
    inputs = (
        torch.rand(1, count0, 2),
        torch.rand(1, count1, 2),
        torch.rand(1, count0),
        torch.rand(1, count1),
        torch.rand(1, DESCRIPTOR_DIM, count0),
        torch.rand(1, DESCRIPTOR_DIM, count1),
    )
    torch.onnx.export(
        superglue,
        inputs,
        superglue_path,
        input_names=[
            "keypoints0",
            "keypoints1",
            "scores0",
            "scores1",
            "descriptors0",
            "descriptors1",
        ],
        output_names=[
            "matches0",
            "matches1",
            "matching_scores0",
            "matching_scores1",
        ],
        dynamic_axes={
            "keypoints0": {1: "keypoints0_count"},
            "keypoints1": {1: "keypoints1_count"},
            "scores0": {1: "keypoints0_count"},
            "scores1": {1: "keypoints1_count"},
            "descriptors0": {2: "keypoints0_count"},
            "descriptors1": {2: "keypoints1_count"},
            "matches0": {1: "keypoints0_count"},
            "matches1": {1: "keypoints1_count"},
            "matching_scores0": {1: "keypoints0_count"},
            "matching_scores1": {1: "keypoints1_count"},
        },
        opset_version=17,
        do_constant_folding=True,
    )


def _torch_dtype(tensorrt: Any, dtype: Any) -> torch.dtype:
    mapping = {
        tensorrt.float32: torch.float32,
        tensorrt.float16: torch.float16,
        tensorrt.int8: torch.int8,
        tensorrt.int32: torch.int32,
        tensorrt.int64: torch.int64,
        tensorrt.bool: torch.bool,
    }
    try:
        return mapping[dtype]
    except KeyError as exc:
        raise TypeError(f"unsupported TensorRT tensor dtype: {dtype}") from exc


class TensorRTEngine:
    """开发期辅助：在 PyTorch CUDA tensor 上直接执行 TensorRT 10 engine。

    当前生产镜像不含 PyTorch，实际运行实现位于 ``superglue_tensorrt_runtime.py``。
    """

    def __init__(self, plan_path: Path) -> None:
        import tensorrt as trt

        self._trt = trt
        self._logger = trt.Logger(trt.Logger.WARNING)
        self._runtime = trt.Runtime(self._logger)
        self._engine = self._runtime.deserialize_cuda_engine(plan_path.read_bytes())
        if self._engine is None:
            raise RuntimeError(f"could not deserialize TensorRT engine {plan_path}")
        self._context = self._engine.create_execution_context()
        if self._context is None:
            raise RuntimeError(f"could not create TensorRT context for {plan_path}")
        self.input_names = tuple(
            self._engine.get_tensor_name(index)
            for index in range(self._engine.num_io_tensors)
            if self._engine.get_tensor_mode(self._engine.get_tensor_name(index))
            == trt.TensorIOMode.INPUT
        )
        self.output_names = tuple(
            self._engine.get_tensor_name(index)
            for index in range(self._engine.num_io_tensors)
            if self._engine.get_tensor_mode(self._engine.get_tensor_name(index))
            == trt.TensorIOMode.OUTPUT
        )

    def __call__(self, inputs: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
        if set(inputs) != set(self.input_names):
            raise ValueError(
                f"TensorRT inputs {sorted(inputs)} differ from {sorted(self.input_names)}"
            )
        bound: dict[str, torch.Tensor] = {}
        for name in self.input_names:
            expected = _torch_dtype(
                self._trt, self._engine.get_tensor_dtype(name)
            )
            tensor = inputs[name].to(device="cuda", dtype=expected).contiguous()
            if not self._context.set_input_shape(name, tuple(tensor.shape)):
                raise ValueError(f"TensorRT rejected {name} shape {tuple(tensor.shape)}")
            bound[name] = tensor

        outputs: dict[str, torch.Tensor] = {}
        for name in self.output_names:
            shape = tuple(self._context.get_tensor_shape(name))
            if any(dimension < 0 for dimension in shape):
                raise RuntimeError(f"TensorRT did not resolve {name} shape: {shape}")
            dtype = _torch_dtype(self._trt, self._engine.get_tensor_dtype(name))
            outputs[name] = torch.empty(shape, dtype=dtype, device="cuda")

        for name, tensor in {**bound, **outputs}.items():
            if not self._context.set_tensor_address(name, tensor.data_ptr()):
                raise RuntimeError(f"could not bind TensorRT tensor {name}")
        stream = torch.cuda.current_stream()
        if not self._context.execute_async_v3(stream.cuda_stream):
            raise RuntimeError("TensorRT execution failed")
        return outputs


class TensorRTMatcher:
    """开发期旧对照实现：TensorRT 主干加 Torch 动态后处理，生产路径不调用。"""

    def __init__(
        self,
        checkout: Path,
        engine_dir: Path,
        *,
        max_keypoints: int,
        keypoint_threshold: float,
    ) -> None:
        import tensorrt

        checkout = checkout.resolve()
        sys.path.insert(0, str(checkout))
        try:
            module = importlib.import_module("models.superpoint")
        finally:
            sys.path.remove(str(checkout))
        self._simple_nms = module.simple_nms
        self._remove_borders = module.remove_borders
        self._top_k_keypoints = module.top_k_keypoints
        self._sample_descriptors = module.sample_descriptors
        self._max_keypoints = int(max_keypoints)
        self._keypoint_threshold = float(keypoint_threshold)
        self._superpoint = TensorRTEngine(engine_dir / "superpoint_dense_fp16.plan")
        self._superglue = TensorRTEngine(engine_dir / "superglue_fp32.plan")
        self._stream = torch.cuda.Stream()
        self.backend_name = "tensorrt"
        self.runtime_version = tensorrt.__version__

    @staticmethod
    def _image_tensor(image: np.ndarray) -> torch.Tensor:
        if image.shape != (IMAGE_HEIGHT, IMAGE_WIDTH):
            raise ValueError(
                f"TensorRT image must be {IMAGE_HEIGHT}x{IMAGE_WIDTH}, got {image.shape}"
            )
        return (
            torch.from_numpy(np.ascontiguousarray(image))
            .cuda(non_blocking=True)
            .float()
            .div_(255.0)[None, None]
        )

    def _extract_cuda(
        self, image: np.ndarray
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        output = self._superpoint({"image": self._image_tensor(image)})
        score_logits = output["score_logits"].float()
        dense_descriptors = output["dense_descriptors"].float()
        scores = F.softmax(score_logits, dim=1)[:, :-1]
        batch, _, height, width = scores.shape
        scores = scores.permute(0, 2, 3, 1).reshape(
            batch, height, width, 8, 8
        )
        scores = scores.permute(0, 1, 3, 2, 4).reshape(
            batch, height * 8, width * 8
        )
        scores = self._simple_nms(scores, 4)[0]
        keypoints = torch.nonzero(scores > self._keypoint_threshold)
        keypoint_scores = scores[tuple(keypoints.t())]
        keypoints, keypoint_scores = self._remove_borders(
            keypoints, keypoint_scores, 4, height * 8, width * 8
        )
        if self._max_keypoints >= 0:
            keypoints, keypoint_scores = self._top_k_keypoints(
                keypoints, keypoint_scores, self._max_keypoints
            )
        keypoints = torch.flip(keypoints, dims=(1,)).float()
        dense_descriptors = F.normalize(dense_descriptors, p=2, dim=1)
        descriptors = self._sample_descriptors(
            keypoints[None], dense_descriptors, 8
        )[0]
        return keypoints, descriptors, keypoint_scores

    def extract(self, image: np.ndarray):
        with torch.inference_mode():
            with torch.cuda.stream(self._stream):
                keypoints, descriptors, scores = self._extract_cuda(image)
            self._stream.synchronize()
        return (
            keypoints.cpu().numpy().astype(np.float32),
            descriptors.t().cpu().numpy().astype(np.float32),
            scores.cpu().numpy().astype(np.float32),
        )

    def warmup(self, height: int = 640, width: int = 544, runs: int = 2) -> None:
        """Prime TensorRT contexts and CUDA kernels before advertising health."""

        yy, xx = np.indices((height, width))
        left = (((xx // 32 + yy // 32) % 2) * 180 + 35).astype(np.uint8)
        right = np.roll(left, -8, axis=1).copy()
        for index in range(max(1, int(runs))):
            started = time.perf_counter()
            result = self.match(left, right)
            print(
                f"TensorRT warmup {index + 1}/{runs}: "
                f"matches={len(result[0])}, "
                f"elapsed_ms={(time.perf_counter() - started) * 1000.0:.2f}",
                flush=True,
            )

    def match(self, left: np.ndarray, right: np.ndarray):
        with torch.inference_mode():
            with torch.cuda.stream(self._stream):
                keypoints0, descriptors0, scores0 = self._extract_cuda(left)
                keypoints1, descriptors1, scores1 = self._extract_cuda(right)
                if not len(keypoints0) or not len(keypoints1):
                    empty_points = np.empty((0, 2), dtype=np.float32)
                    empty_descriptors = np.empty(
                        (0, DESCRIPTOR_DIM), dtype=np.float32
                    )
                    empty_scores = np.empty((0,), dtype=np.float32)
                    self._stream.synchronize()
                    return (
                        empty_points,
                        empty_points.copy(),
                        empty_descriptors,
                        empty_scores,
                        len(keypoints0),
                        len(keypoints1),
                    )
                output = self._superglue(
                    {
                        "keypoints0": keypoints0[None],
                        "keypoints1": keypoints1[None],
                        "scores0": scores0[None],
                        "scores1": scores1[None],
                        "descriptors0": descriptors0[None],
                        "descriptors1": descriptors1[None],
                    }
                )
                matches0 = output["matches0"][0]
                matching_scores0 = output["matching_scores0"][0]
                left_indices = torch.nonzero(matches0 >= 0).reshape(-1)
                right_indices = matches0[left_indices].long()
                selected = (
                    keypoints0[left_indices],
                    keypoints1[right_indices],
                    descriptors0[:, left_indices].t(),
                    matching_scores0[left_indices],
                )
            self._stream.synchronize()
        return (
            selected[0].cpu().numpy().astype(np.float32),
            selected[1].cpu().numpy().astype(np.float32),
            selected[2].cpu().numpy().astype(np.float32),
            selected[3].cpu().numpy().astype(np.float32),
            len(keypoints0),
            len(keypoints1),
        )
