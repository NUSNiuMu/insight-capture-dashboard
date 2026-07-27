#!/usr/bin/env python3

"""Export the pinned official models during the PyTorch builder stage."""

from __future__ import annotations

import argparse
from pathlib import Path

from superglue_tensorrt import export_onnx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", default="/opt/SuperGluePretrainedNetwork")
    parser.add_argument("--output-dir", default="/opt/insight/onnx")
    parser.add_argument("--weights", choices=("indoor", "outdoor"), default="indoor")
    parser.add_argument("--max-keypoints", type=int, default=1024)
    parser.add_argument("--keypoint-threshold", type=float, default=0.005)
    parser.add_argument("--match-threshold", type=float, default=0.2)
    parser.add_argument("--sinkhorn-iterations", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    export_onnx(
        Path(args.checkout),
        Path(args.output_dir),
        weights=args.weights,
        max_keypoints=args.max_keypoints,
        keypoint_threshold=args.keypoint_threshold,
        match_threshold=args.match_threshold,
        sinkhorn_iterations=args.sinkhorn_iterations,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
