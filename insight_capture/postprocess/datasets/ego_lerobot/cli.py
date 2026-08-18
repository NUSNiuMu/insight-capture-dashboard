#!/usr/bin/env python3
"""Convert a three-view Insight ROS bag into the Ego LeRobot delivery format."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from insight_capture.postprocess.datasets.ego_lerobot import ExportOptions, export_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--spec", type=Path, required=True, help="crop and action-segment JSON")
    parser.add_argument("--camera-config", type=Path, default=PROJECT_ROOT / "config/cameras.json")
    parser.add_argument("--cache-root", type=Path, default=PROJECT_ROOT / "outputs/ego_lerobot_cache")
    parser.add_argument("--hand-backend", default="wilor", help="wilor or module:factory")
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--hand-confidence", type=float, default=0.3)
    parser.add_argument("--max-image-skew-ms", type=float, default=25.0)
    parser.add_argument("--max-pose-bracket-ms", type=float, default=100.0)
    parser.add_argument("--projection-gate-px", type=float, default=600.0)
    parser.add_argument("--temporal-same-step-m", type=float, default=0.15)
    parser.add_argument("--temporal-advantage-m", type=float, default=0.08)
    parser.add_argument("--reuse-dataset", type=Path, help="validate and seed cache from an already accepted delivery")
    parser.add_argument("--reuse-overlay", type=Path)
    parser.add_argument("--no-overlay", action="store_true")
    parser.add_argument("--decode-audit", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    options = ExportOptions(
        bag=args.bag.resolve(), output=args.output.resolve(), spec_path=args.spec.resolve(),
        camera_config=args.camera_config.resolve(), cache_root=args.cache_root.resolve(),
        hand_backend=args.hand_backend, model_dir=args.model_dir.resolve() if args.model_dir else None,
        hand_confidence=args.hand_confidence, max_image_skew_ms=args.max_image_skew_ms,
        max_pose_bracket_ms=args.max_pose_bracket_ms, projection_gate_px=args.projection_gate_px,
        temporal_same_step_m=args.temporal_same_step_m, temporal_advantage_m=args.temporal_advantage_m,
        reuse_dataset=args.reuse_dataset.resolve() if args.reuse_dataset else None,
        reuse_overlay=args.reuse_overlay.resolve() if args.reuse_overlay else None,
        write_review_overlay=not args.no_overlay, decode_audit=args.decode_audit,
    )
    print(json.dumps(export_dataset(options), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
