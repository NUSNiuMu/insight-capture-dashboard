"""Build-time ONNX export of the official SuperPoint/SuperGlue models."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

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
