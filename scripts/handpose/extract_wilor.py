#!/usr/bin/env python3
"""Extract and stabilize WiLoR camera-space hand poses from a rosbag."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys

import cv2
import numpy as np
import torch
from rosbags.highlevel import AnyReader
from rosbags.typesys import Stores, get_typestore
from wilor_mini.pipelines.wilor_hand_pose3d_estimation_pipeline import (
    WiLorHandPose3dEstimationPipeline,
)

from handpose.one_euro import stabilize_wilor
from handpose.schema import DEFAULT_IMAGE_TOPIC


FOCAL_LENGTH = 768.3264582796221 * 256.0 / 1920.0
DEFAULT_MODEL_DIR = Path("/opt/insight/models/wilor")
MANO_SHAPE_NOTICE = (
    "WARNING: You are using a MANO model, with only 10 shape coefficients."
)


class _KnownNoticeFilter:
    """Drop one expected MANO notice while preserving all other output."""

    def __init__(self, stream) -> None:
        self.stream = stream

    def write(self, text: str) -> int:
        if MANO_SHAPE_NOTICE in text:
            return len(text)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag_dir", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("--topic", default=DEFAULT_IMAGE_TOPIC)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--hand-confidence", type=float, default=0.3)
    parser.add_argument("--min-cutoff", type=float, default=1.5)
    parser.add_argument("--beta", type=float, default=0.3)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(
            os.environ.get("HANDPOSE_WILOR_MODEL_DIR", DEFAULT_MODEL_DIR)
        ),
    )
    args = parser.parse_args()

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    typestore = get_typestore(Stores.ROS2_HUMBLE)
    raw_records = []
    frame_count = 0
    first_stamp_ns = None
    with AnyReader([args.bag_dir], default_typestore=typestore) as reader:
        connections = [
            connection
            for connection in reader.connections
            if connection.topic == args.topic
        ]
        if not connections:
            raise RuntimeError(f"Topic {args.topic!r} is not present in the bag")
        total_frames = sum(connection.msgcount for connection in connections)
        if args.max_frames > 0:
            total_frames = min(total_frames, args.max_frames)
        print(f"HANDPOSE_PROGRESS 0 0 {total_frames}", flush=True)

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        data_type = torch.float16 if device.type == "cuda" else torch.float32
        with redirect_stdout(_KnownNoticeFilter(sys.stdout)):
            pipeline = WiLorHandPose3dEstimationPipeline(
                device=device,
                dtype=data_type,
                focal_length=FOCAL_LENGTH,
                wilor_pretrained_dir=str(args.model_dir),
                verbose=False,
            )
        for connection, _bag_timestamp, rawdata in reader.messages(
            connections=connections
        ):
            frame_count += 1
            message = reader.deserialize(rawdata, connection.msgtype)
            frame = cv2.imdecode(
                np.frombuffer(message.data, dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            if frame is None:
                print(
                    "HANDPOSE_PROGRESS "
                    f"{frame_count} {len(raw_records)} {total_frames}",
                    flush=True,
                )
                if args.max_frames > 0 and frame_count >= args.max_frames:
                    break
                continue
            stamp_ns = (
                int(message.header.stamp.sec) * 1_000_000_000
                + int(message.header.stamp.nanosec)
            )
            if first_stamp_ns is None:
                first_stamp_ns = stamp_ns
            elapsed_ms = int((stamp_ns - first_stamp_ns) // 1_000_000)
            hands = []
            for output in pipeline.predict(
                frame, hand_conf=args.hand_confidence
            ):
                prediction = output.get("wilor_preds")
                if not prediction:
                    continue
                local_points = prediction["pred_keypoints_3d"][0]
                translation = prediction["pred_cam_t_full"][0]
                camera_points = local_points + translation[None, :]
                hands.append(
                    {
                        "c": "R" if bool(output["is_right"]) else "L",
                        "s": 1.0,
                        "p": [
                            round(float(value), 4)
                            for value in camera_points.flatten()
                        ],
                    }
                )
            if hands:
                raw_records.append({"t": elapsed_ms, "h": hands})
            print(
                "HANDPOSE_PROGRESS "
                f"{frame_count} {len(raw_records)} {total_frames}",
                flush=True,
            )
            if args.max_frames > 0 and frame_count >= args.max_frames:
                break

    records = stabilize_wilor(
        raw_records,
        min_cutoff=args.min_cutoff,
        beta=args.beta,
    )
    with args.output_json.open("w", encoding="utf-8") as stream:
        json.dump(records, stream, separators=(",", ":"))
    print(
        "HANDPOSE_DONE "
        f"{frame_count} {len(records)} {total_frames} {args.output_json}",
        flush=True,
    )


if __name__ == "__main__":
    main()
