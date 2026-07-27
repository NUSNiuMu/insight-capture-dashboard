#!/usr/bin/env python3

"""Prepare and validate the device-specific SuperPoint/SuperGlue engine cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path

import tensorrt
import torch

from superglue_tensorrt import (
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    OFFICIAL_COMMIT,
    export_onnx,
)


PLAN_NAMES = ("superpoint_dense_fp16.plan", "superglue_fp32.plan")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_manifest(args: argparse.Namespace) -> dict[str, object]:
    checkout_commit = subprocess.check_output(
        ["git", "-C", args.checkout, "rev-parse", "HEAD"], text=True
    ).strip()
    if checkout_commit != OFFICIAL_COMMIT:
        raise RuntimeError(
            f"official checkout is {checkout_commit}, expected {OFFICIAL_COMMIT}"
        )
    capability = torch.cuda.get_device_capability(0)
    return {
        "schema": 1,
        "official_commit": OFFICIAL_COMMIT,
        "weights": args.weights,
        "max_keypoints": args.max_keypoints,
        "keypoint_threshold": args.keypoint_threshold,
        "match_threshold": args.match_threshold,
        "sinkhorn_iterations": args.sinkhorn_iterations,
        "image_height": IMAGE_HEIGHT,
        "image_width": IMAGE_WIDTH,
        "tensorrt": tensorrt.__version__,
        "cuda": torch.version.cuda,
        "compute_capability": list(capability),
        "machine": platform.machine(),
    }


def check(args: argparse.Namespace) -> int:
    cache = Path(args.engine_dir)
    manifest_path = cache / "manifest.json"
    if not manifest_path.is_file():
        print("TensorRT cache miss: manifest is absent")
        return 1
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"TensorRT cache miss: invalid manifest: {exc}")
        return 1
    expected = expected_manifest(args)
    recorded = {key: manifest.get(key) for key in expected}
    if recorded != expected:
        print("TensorRT cache miss: build identity changed")
        return 1
    for name in PLAN_NAMES:
        path = cache / name
        if not path.is_file() or path.stat().st_size <= 0:
            print(f"TensorRT cache miss: {name} is absent")
            return 1
        if manifest.get("sha256", {}).get(name) != _sha256(path):
            print(f"TensorRT cache miss: {name} checksum differs")
            return 1
    print(
        f"TensorRT cache ready: TRT {expected['tensorrt']}, "
        f"SM {''.join(str(value) for value in expected['compute_capability'])}"
    )
    return 0


def export(args: argparse.Namespace) -> int:
    cache = Path(args.engine_dir)
    export_onnx(
        Path(args.checkout),
        cache,
        weights=args.weights,
        max_keypoints=args.max_keypoints,
        keypoint_threshold=args.keypoint_threshold,
        match_threshold=args.match_threshold,
        sinkhorn_iterations=args.sinkhorn_iterations,
    )
    print(f"exported TensorRT ONNX sources to {cache}")
    return 0


def finalize(args: argparse.Namespace) -> int:
    cache = Path(args.engine_dir)
    manifest = expected_manifest(args)
    manifest["sha256"] = {
        name: _sha256(cache / name)
        for name in PLAN_NAMES
    }
    temporary = cache / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    temporary.replace(cache / "manifest.json")
    return check(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "export", "finalize"))
    parser.add_argument("--checkout", default="/opt/SuperGluePretrainedNetwork")
    parser.add_argument("--engine-dir", default="/opt/insight/engines")
    parser.add_argument("--weights", choices=("indoor", "outdoor"), default="indoor")
    parser.add_argument("--max-keypoints", type=int, default=1024)
    parser.add_argument("--keypoint-threshold", type=float, default=0.005)
    parser.add_argument("--match-threshold", type=float, default=0.2)
    parser.add_argument("--sinkhorn-iterations", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return {
        "check": check,
        "export": export,
        "finalize": finalize,
    }[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
