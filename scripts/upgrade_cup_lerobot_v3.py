#!/usr/bin/env python3

"""Upgrade cup LeRobot v3 datasets for OpenPI chunks and gripper validity."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cup_lerobot_pipeline import upgrade_existing_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("datasets", nargs="+", type=Path)
    parser.add_argument(
        "--skip-video-decode",
        action="store_true",
        help="only inspect video metadata instead of decoding every frame",
    )
    parser.add_argument("--no-review", action="store_true")
    args = parser.parse_args()
    failed = False
    for dataset in args.datasets:
        try:
            result = upgrade_existing_dataset(
                dataset,
                decode_videos=not args.skip_video_decode,
                write_review=not args.no_review,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            failed = True
            print(f"UPGRADE_FAILED {dataset}: {exc}", file=sys.stderr)
            continue
        print(
            "UPGRADE_DONE "
            + json.dumps(
                {
                    "dataset": str(dataset.resolve()),
                    "episodes": result["episodes"],
                    "frames": result["frames"],
                    "minimum_segment_frames": result["minimum_segment_frames"],
                    "gripper_width_quality": result["gripper_width_quality"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
