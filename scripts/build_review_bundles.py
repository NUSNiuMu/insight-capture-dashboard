#!/usr/bin/env python3
"""Prebuild synchronized review bundles without entering the recording path."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from camera_setup import build_dashboard_config, load_setup
from post_processing_core.prepared_playback import PreparedPlaybackManager


class _RecordingGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.processes = {}
        self.merge_state = "done"
        self.recording = False

    def _cleanup_if_exited_unlocked(self) -> None:
        return

    def is_recording(self) -> bool:
        return self.recording


def _dashboard_recording(api_root: str) -> bool | None:
    try:
        with urlopen(f"{api_root.rstrip('/')}/api/recording/status", timeout=2.0) as response:
            payload = json.load(response)
        return bool(payload.get("recording")) or payload.get("merge_state") == "merging"
    except (OSError, URLError, ValueError):
        return None


def _playback_configuration(config_path: Path) -> tuple[list[dict], list[dict]]:
    dashboard = build_dashboard_config(load_setup(config_path))
    pose_by_name = {item["name"]: item for item in dashboard["poses"]}
    cameras = []
    for camera in dashboard["cameras"]:
        pose = pose_by_name[camera["name"]]
        cameras.append(
            {
                "name": camera["name"],
                "label": camera["label"],
                "topic": camera["topic"],
                "role": pose["teleop_role"],
                "rotation_deg": camera["rotation_deg"],
                "row": camera["row"],
                "column": camera["column"],
            }
        )
    poses = [
        {
            "name": pose["name"],
            "topic": pose["topic"],
            "role": pose["teleop_role"],
            "avatar_model": pose["avatar_model"],
            "avatar_scale": pose["avatar_scale"],
            "avatar_rotation_deg_xyz": list(pose["avatar_rotation_deg_xyz"]),
            "avatar_offset_xyz": list(pose["avatar_offset_xyz"]),
        }
        for pose in dashboard["poses"]
    ]
    return cameras, poses


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag_names", nargs="*", help="rosbag directory names")
    parser.add_argument("--all", action="store_true", help="build every bag with metadata.yaml")
    parser.add_argument("--config", type=Path, default=project_root / "config" / "cameras.json")
    parser.add_argument("--rosbag-root", type=Path, default=project_root / "rosbags")
    parser.add_argument("--api-root", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="run without the dashboard safety check only after capture is stopped",
    )
    parser.add_argument("--software", action="store_true", help="force libx264 instead of Jetson NVENC")
    args = parser.parse_args()
    if not args.all and not args.bag_names:
        parser.error("provide one or more bag names, or --all")
    if args.software:
        os.environ["INSIGHT_REVIEW_SOFTWARE_ENCODER"] = "1"

    rosbag_root = args.rosbag_root.resolve()
    if args.all:
        bag_names = [
            path.name
            for path in sorted(rosbag_root.iterdir(), key=lambda item: item.stat().st_mtime)
            if path.is_dir() and (path / "metadata.yaml").is_file()
        ]
    else:
        bag_names = list(dict.fromkeys(args.bag_names))
    cameras, poses = _playback_configuration(args.config.resolve())
    guard = _RecordingGuard()
    manager = PreparedPlaybackManager(rosbag_root, project_root / "outputs" / "results" / "playback_cache")

    for index, bag_name in enumerate(bag_names, 1):
        dashboard_busy = False if args.offline else _dashboard_recording(args.api_root)
        if dashboard_busy is None:
            print("Dashboard status is unavailable; stop capture and use --offline to proceed.")
            return 2
        if dashboard_busy:
            print("Recording or merge is active; review generation stopped.")
            return 2
        print(f"[{index}/{len(bag_names)}] {bag_name}")
        status = manager.prepare(bag_name, guard, cameras, poses)
        while status["state"] == "preparing":
            dashboard_busy = False if args.offline else _dashboard_recording(args.api_root)
            if dashboard_busy is None or dashboard_busy:
                guard.recording = True
            print(
                f"  {float(status.get('progress', 0)) * 100:5.1f}% "
                f"{status.get('stage', '')}",
                end="\r",
                flush=True,
            )
            time.sleep(0.5)
            status = manager.status()
        print(" " * 100, end="\r")
        if guard.recording:
            print("Recording started; review generation paused without changing the rosbag.")
            return 2
        if status["state"] == "error":
            print(f"  ERROR: {status.get('error', 'unknown error')}")
            continue
        print(f"  ready: {rosbag_root / bag_name / 'review' / 'review.mp4'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
