#!/usr/bin/env python3
"""Extract compact MediaPipe hand poses from an existing rosbag."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    HandLandmarker,
    HandLandmarkerOptions,
    RunningMode,
)
from rosbags.highlevel import AnyReader
from rosbags.typesys import Stores, get_typestore

from handpose.one_euro import stabilize_mediapipe
from handpose.schema import DEFAULT_IMAGE_TOPIC


HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
)


def _draw(frame: np.ndarray, points: list, color: tuple) -> None:
    for start, end in HAND_CONNECTIONS:
        cv2.line(frame, points[start], points[end], color, 2, cv2.LINE_AA)
    for point in points:
        cv2.circle(frame, point, 3, (255, 255, 255), -1, cv2.LINE_AA)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bag_dir", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--topic", default=DEFAULT_IMAGE_TOPIC)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--max-hands", type=int, default=2)
    parser.add_argument("--detection-confidence", type=float, default=0.5)
    parser.add_argument("--tracking-confidence", type=float, default=0.5)
    parser.add_argument("--min-cutoff", type=float, default=1.5)
    parser.add_argument("--beta", type=float, default=0.3)
    parser.add_argument("--confirmation-frames", type=int, default=2)
    args = parser.parse_args()

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)

    options = HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(args.model)),
        running_mode=RunningMode.VIDEO,
        num_hands=max(1, min(args.max_hands, 2)),
        min_hand_detection_confidence=args.detection_confidence,
        min_tracking_confidence=args.tracking_confidence,
    )
    typestore = get_typestore(Stores.ROS2_HUMBLE)
    raw_records = []
    frame_count = 0
    detected_frame_count = 0
    first_stamp_ns = None
    writer = None

    with AnyReader([args.bag_dir], default_typestore=typestore) as reader:
        connections = [
            connection
            for connection in reader.connections
            if connection.topic == args.topic
        ]
        if not connections:
            raise RuntimeError(f"Topic {args.topic!r} is not present in the bag")

        with HandLandmarker.create_from_options(options) as landmarker:
            for connection, _bag_timestamp, rawdata in reader.messages(
                connections=connections
            ):
                message = reader.deserialize(rawdata, connection.msgtype)
                frame = cv2.imdecode(
                    np.frombuffer(message.data, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if frame is None:
                    continue
                stamp_ns = (
                    int(message.header.stamp.sec) * 1_000_000_000
                    + int(message.header.stamp.nanosec)
                )
                if first_stamp_ns is None:
                    first_stamp_ns = stamp_ns
                elapsed_ms = int((stamp_ns - first_stamp_ns) // 1_000_000)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                    elapsed_ms,
                )

                hands = []
                height, width = frame.shape[:2]
                for index, (landmarks, handedness) in enumerate(
                    zip(result.hand_landmarks, result.handedness)
                ):
                    if (
                        not result.hand_world_landmarks
                        or index >= len(result.hand_world_landmarks)
                    ):
                        continue
                    category = handedness[0]
                    points = []
                    for landmark in result.hand_world_landmarks[index]:
                        points.extend(
                            round(float(value), 4)
                            for value in (landmark.x, landmark.y, landmark.z)
                        )
                    hands.append(
                        {
                            "c": category.category_name[:1].upper(),
                            "s": round(float(category.score), 3),
                            "p": points,
                            "a": [
                                round(float(landmarks[0].x), 5),
                                round(float(landmarks[0].y), 5),
                            ],
                        }
                    )
                    if args.preview:
                        pixels = [
                            (int(landmark.x * width), int(landmark.y * height))
                            for landmark in landmarks
                        ]
                        color = (
                            (35, 126, 194)
                            if category.category_name.startswith("R")
                            else (151, 113, 42)
                        )
                        _draw(frame, pixels, color)
                raw_records.append({"t": elapsed_ms, "h": hands})
                if hands:
                    detected_frame_count += 1

                if args.preview:
                    if writer is None:
                        writer = cv2.VideoWriter(
                            str(args.preview),
                            cv2.VideoWriter_fourcc(*"mp4v"),
                            30.0,
                            (width, height),
                        )
                    writer.write(frame)

                frame_count += 1
                if frame_count % 50 == 0:
                    print(
                        f"HANDPOSE_PROGRESS {frame_count} {detected_frame_count}",
                        flush=True,
                    )
                if args.max_frames > 0 and frame_count >= args.max_frames:
                    break

    if writer is not None:
        writer.release()
    records = stabilize_mediapipe(
        raw_records,
        min_cutoff=args.min_cutoff,
        beta=args.beta,
        confirmation_frames=args.confirmation_frames,
    )
    with args.output_json.open("w", encoding="utf-8") as stream:
        json.dump(records, stream, separators=(",", ":"))
    print(
        f"HANDPOSE_DONE {frame_count} {len(records)} {args.output_json}",
        flush=True,
    )


if __name__ == "__main__":
    main()
