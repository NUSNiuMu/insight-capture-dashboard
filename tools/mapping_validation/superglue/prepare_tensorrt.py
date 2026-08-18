#!/usr/bin/env python3

"""Stage and validate the device-specific pure TensorRT engine cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
from pathlib import Path
import sys

PROJECT_ROOT = next(
    (parent for parent in Path(__file__).resolve().parents if (parent / "insight_capture").is_dir()),
    None,
)
if PROJECT_ROOT is not None:
    sys.path.insert(0, str(PROJECT_ROOT))

import tensorrt

try:
    from insight_capture.runtime.mapping.superglue_tensorrt_runtime import (
        CudaRuntime, IMAGE_HEIGHT, IMAGE_WIDTH, OFFICIAL_COMMIT,
    )
except ModuleNotFoundError:  # isolated SuperGlue runtime image
    from superglue_tensorrt_runtime import (
        CudaRuntime, IMAGE_HEIGHT, IMAGE_WIDTH, OFFICIAL_COMMIT,
    )


ONNX_NAMES = ("superpoint.onnx", "superglue.onnx")
PLAN_NAMES = ("superpoint_fp16.plan", "superglue_fp32.plan")
MODEL_PARAMETERS = {
    "weights": "indoor",
    "max_keypoints": 1024,
    "keypoint_threshold": 0.005,
    "match_threshold": 0.2,
    "sinkhorn_iterations": 20,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_manifest(args: argparse.Namespace) -> dict[str, object]:
    cuda = CudaRuntime()
    try:
        capability = cuda.compute_capability
        cuda_version = cuda.runtime_version
    finally:
        cuda.close()
    onnx_dir = Path(args.onnx_dir)
    return {
        "schema": 2,
        "runtime": "tensorrt-cuda",
        "official_commit": OFFICIAL_COMMIT,
        **MODEL_PARAMETERS,
        "image_height": IMAGE_HEIGHT,
        "image_width": IMAGE_WIDTH,
        "tensorrt": tensorrt.__version__,
        "cuda": cuda_version,
        "compute_capability": list(capability),
        "machine": platform.machine(),
        "onnx_sha256": {
            name: _sha256(onnx_dir / name)
            for name in ONNX_NAMES
        },
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
        if manifest.get("plan_sha256", {}).get(name) != _sha256(path):
            print(f"TensorRT cache miss: {name} checksum differs")
            return 1
    print(
        f"TensorRT/CUDA cache ready: TRT {expected['tensorrt']}, "
        f"SM {''.join(str(value) for value in expected['compute_capability'])}"
    )
    return 0


def stage(args: argparse.Namespace) -> int:
    source = Path(args.onnx_dir)
    cache = Path(args.engine_dir)
    cache.mkdir(parents=True, exist_ok=True)
    for name in ONNX_NAMES:
        shutil.copyfile(source / name, cache / name)
    print(f"staged pre-exported ONNX sources in {cache}")
    return 0


def finalize(args: argparse.Namespace) -> int:
    cache = Path(args.engine_dir)
    manifest = expected_manifest(args)
    manifest["plan_sha256"] = {
        name: _sha256(cache / name)
        for name in PLAN_NAMES
    }
    temporary = cache / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    temporary.replace(cache / "manifest.json")
    return check(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "stage", "finalize"))
    parser.add_argument("--onnx-dir", default="/opt/insight/onnx")
    parser.add_argument("--engine-dir", default="/opt/insight/engines")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return {
        "check": check,
        "stage": stage,
        "finalize": finalize,
    }[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
