"""Evaluate recorded metric poses against one fixed ChArUco board, offline."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from rosbags.highlevel import AnyReader
from rosbags.typesys import Stores, get_typestore

from insight_capture.postprocess.gripper.extraction import (
    IMAGE_TYPES,
    decode_color_image,
    header_stamp_ns,
)
from insight_capture.runtime.mapping.geometry import (
    PoseSample,
    interpolate_pose,
    matrix_from_pose,
    rotation_distance_deg,
)

# These are measurement-quality gates, not product acceptance thresholds.
PERIOD_NS = 200_000_000
MAX_POSE_GAP_NS = 50_000_000
MIN_CORNERS = 12
MAX_REPROJECTION_PX = 1.0
MIN_PLANAR_GAP_PX = 0.15


def rigid(value: object) -> np.ndarray:
    """Validate a finite, proper SE(3) transform without repairing it."""
    matrix = np.asarray(value, dtype=float)
    if (
        matrix.shape != (4, 4)
        or not np.isfinite(matrix).all()
        or not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-7)
        or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-6)
        or not np.isclose(np.linalg.det(matrix[:3, :3]), 1, atol=1e-6)
    ):
        raise ValueError("expected a finite 4x4 SE(3) matrix (metres, no scale)")
    return matrix


def windows(config: dict) -> list[tuple[str, float, float]]:
    return [("reference", *config["reference_interval_s"])] + [
        (item["name"], item["start_s"], item["end_s"])
        for item in config["evaluation_intervals"]
    ]


def load_config(path: Path, *, evaluation: bool) -> dict:
    config = json.loads(path.read_text())
    if config["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    board = config["board"]
    if (
        board["dictionary"] != "DICT_4X4_50"
        or any(
            type(board[key]) is not int or board[key] < 3
            for key in ("squares_x", "squares_y")
        )
        or (board["squares_x"] - 1) * (board["squares_y"] - 1) < MIN_CORNERS
        or board["squares_x"] * board["squares_y"] // 2 > 50
        or not 0 < board["marker_length_m"] < board["square_length_m"]
        or not math.isfinite(board["square_length_m"])
    ):
        raise ValueError("invalid board geometry; only DICT_4X4_50 is supported")
    intervals = windows(config)
    if len(intervals) < 2 or len({item[0] for item in intervals}) != len(intervals):
        raise ValueError("provide uniquely named evaluation intervals")
    previous = 0.0
    for name, start, end in intervals:
        if (
            not isinstance(name, str)
            or not name
            or not (math.isfinite(start) and math.isfinite(end))
            or not previous <= start < end
        ):
            raise ValueError(
                "intervals must be finite, ordered, non-overlapping and >= 0"
            )
        previous = end
    camera = config["camera"]
    for key in (
        "image_topic",
        "camera_info_topic",
        "pose_topic",
        "image_frame",
        "pose_world_frame",
        "pose_body_frame",
    ):
        if not isinstance(camera[key], str) or not camera[key].strip():
            raise ValueError(f"camera.{key} must be explicit")
    if evaluation:
        if (
            camera["pose_from_rectified_image"] is None
            or not camera["extrinsic_source"]
        ):
            raise ValueError(
                "missing verified pose_from_rectified_image / extrinsic_source; RGB and stereo centre must not be assumed coincident"
            )
        rigid(camera["pose_from_rectified_image"])
    acceptance = config["acceptance"]
    if acceptance is not None:
        if set(acceptance) != {
            "position_p95_mm",
            "rotation_p95_deg",
            "min_valid_fraction",
        }:
            raise ValueError(
                "acceptance requires position_p95_mm, rotation_p95_deg, min_valid_fraction"
            )
        if (
            any(not math.isfinite(v) or v <= 0 for v in acceptance.values())
            or acceptance["min_valid_fraction"] > 1
        ):
            raise ValueError(
                "acceptance values must be positive and min_valid_fraction <= 1"
            )
    return config


def make_board(config: dict) -> cv2.aruco.CharucoBoard:
    spec = config["board"]
    return cv2.aruco.CharucoBoard(
        (spec["squares_x"], spec["squares_y"]),
        spec["square_length_m"],
        spec["marker_length_m"],
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),
    )


def pyplot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def print_board(config: dict, output: Path) -> None:
    spec = config["board"]
    width = spec["squares_x"] * spec["square_length_m"] * 1000
    height = spec["squares_y"] * spec["square_length_m"] * 1000
    if width > 360 or height > 230:
        raise ValueError("board exceeds A3 printable area (360 x 230 mm)")
    output.mkdir(parents=True, exist_ok=False)
    pixels = make_board(config).generateImage(
        (spec["squares_x"] * 400, spec["squares_y"] * 400)
    )
    cv2.imwrite(str(output / "board.png"), pixels)
    plt = pyplot()
    fig = plt.figure(figsize=(420 / 25.4, 297 / 25.4))
    axis = fig.add_axes(
        [(420 - width) / 840, (297 - height) / 594, width / 420, height / 297]
    )
    axis.imshow(
        pixels, cmap="gray", vmin=0, vmax=255, interpolation="nearest", aspect="auto"
    )
    axis.set_axis_off()
    fig.text(
        0.5,
        0.95,
        f"ChArUco {spec['squares_x']} x {spec['squares_y']}; square {spec['square_length_m'] * 1000:g} mm; marker {spec['marker_length_m'] * 1000:g} mm; DICT_4X4_50",
        ha="center",
    )
    fig.text(
        0.5,
        0.91,
        "A3 / 100% actual size / no fit-to-page / measure both axes before use",
        ha="center",
    )
    scale = fig.add_axes([0.5 - 50 / 420, 0.045, 100 / 420, 0.025])
    scale.plot([0, 1], [0.5, 0.5], "k|-", linewidth=1)
    scale.set(xlim=(0, 1), ylim=(0, 1))
    scale.set_axis_off()
    fig.text(0.5, 0.025, "100 mm", ha="center")
    fig.savefig(output / "board_A3.pdf")
    plt.close(fig)


def reader_for(bag: Path) -> AnyReader:
    if not bag.is_dir() or not (bag / "metadata.yaml").is_file():
        raise ValueError(
            "--bag must be one finalized rosbag2 directory with metadata.yaml"
        )
    return AnyReader([bag], default_typestore=get_typestore(Stores.ROS2_HUMBLE))


def camera_calibration(
    message: object, camera: dict
) -> tuple[np.ndarray, tuple[int, int]]:
    projection = np.asarray(message.p, dtype=float).reshape(3, 4)
    k = projection[:, :3]
    rectification = np.eye(4)
    rectification[:3, :3] = np.asarray(message.r).reshape(3, 3)
    rigid(rectification)
    if (
        message.header.frame_id != camera["image_frame"]
        or message.width <= 0
        or message.height <= 0
    ):
        raise ValueError(
            "CameraInfo frame/dimensions do not match configured rectified image"
        )
    if (
        not np.isfinite(projection).all()
        or k[0, 0] <= 0
        or k[1, 1] <= 0
        or not np.allclose(k[2], [0, 0, 1])
        or not np.allclose(projection[:, 3], 0)
    ):
        raise ValueError(
            "expected rectified pinhole CameraInfo.P with zero fourth column"
        )
    return k, (int(message.height), int(message.width))


def scan(
    reader: AnyReader, config: dict
) -> tuple[np.ndarray, tuple[int, int], list[PoseSample], dict]:
    camera = config["camera"]
    expected = {
        camera["image_topic"]: IMAGE_TYPES,
        camera["camera_info_topic"]: {"sensor_msgs/msg/CameraInfo"},
        camera["pose_topic"]: {"geometry_msgs/msg/PoseStamped"},
    }
    for topic, types in expected.items():
        connections = [item for item in reader.connections if item.topic == topic]
        if not connections or any(item.msgtype not in types for item in connections):
            raise ValueError(
                f"missing or unsupported topic: {topic}; expected {sorted(types)}"
            )
    poses = []
    calibration = None
    fingerprint = None
    last = {}
    counts = Counter()
    ranges = {}
    selected = [item for item in reader.connections if item.topic in expected]
    for connection, _, raw in reader.messages(connections=selected):
        message = reader.deserialize(raw, connection.msgtype)
        topic = connection.topic
        stamp = header_stamp_ns(message)
        if stamp <= 0 or (
            topic != camera["camera_info_topic"] and stamp <= last.get(topic, -1)
        ):
            raise ValueError(
                f"non-positive or non-increasing source header on {topic}; do not substitute bag timestamps"
            )
        last[topic] = stamp
        counts[topic] += 1
        ranges.setdefault(topic, [stamp, stamp])[1] = stamp
        if topic == camera["image_topic"]:
            if message.header.frame_id != camera["image_frame"]:
                raise ValueError("image frame does not match configuration")
            if connection.msgtype.endswith("CompressedImage") and not any(
                kind in message.format.lower() for kind in ("jpeg", "png")
            ):
                raise ValueError(
                    f"unsupported compressed image format: {message.format}; require JPEG/PNG, no video decoding fallback"
                )
        elif topic == camera["camera_info_topic"]:
            current = camera_calibration(message, camera)
            signature = (
                current[1],
                tuple(message.p),
                tuple(message.r),
                tuple(message.d),
                message.distortion_model,
            )
            if fingerprint is not None and signature != fingerprint:
                raise ValueError(
                    "CameraInfo changed inside the run; evaluate separate stable-calibration runs"
                )
            calibration, fingerprint = current, signature
        else:
            if message.header.frame_id != camera["pose_world_frame"]:
                raise ValueError(
                    "pose world frame changed or differs from configuration"
                )
            p, q = message.pose.position, message.pose.orientation
            sample = PoseSample(
                stamp, np.array([p.x, p.y, p.z]), np.array([q.x, q.y, q.z, q.w])
            )
            if not np.isfinite(sample.translation).all() or not np.isclose(
                np.linalg.norm(sample.orientation_xyzw), 1, atol=1e-3
            ):
                raise ValueError(
                    "pose must have finite translation and a unit quaternion"
                )
            matrix_from_pose(sample)
            poses.append(sample)
    if calibration is None or len(poses) < 2:
        raise ValueError("require recorded CameraInfo and at least two poses")
    inventory = {
        "counts": dict(counts),
        "source_header_ranges_ns": ranges,
        "K_rectified": calibration[0].tolist(),
        "image_size_hw": list(calibration[1]),
        "pose_child_frame_contract": camera["pose_body_frame"],
        "extrinsic_present": camera["pose_from_rectified_image"] is not None,
    }
    return *calibration, poses, inventory


def board_pose(
    image: np.ndarray,
    board: cv2.aruco.CharucoBoard,
    detector: cv2.aruco.CharucoDetector,
    k: np.ndarray,
) -> tuple[str, dict, np.ndarray | None]:
    corners, ids, _, _ = detector.detectBoard(image)
    details = {"corners": 0 if ids is None else len(ids)}
    if ids is None or len(ids) < MIN_CORNERS or board.checkCharucoCornersCollinear(ids):
        return "insufficient_corners", details, None
    area = cv2.contourArea(cv2.convexHull(corners)) / (image.shape[0] * image.shape[1])
    if area < 0.002:
        return "board_too_small", details, None
    objects, pixels = board.matchImagePoints(corners, ids)
    result = cv2.solvePnPGeneric(
        objects, pixels, k, np.zeros(5), flags=cv2.SOLVEPNP_IPPE
    )
    candidates = []
    for rvec, tvec in zip(result[1], result[2]):
        rotation = cv2.Rodrigues(rvec)[0]
        if not np.isfinite(rotation).all() or not np.isfinite(tvec).all():
            continue
        camera_points = objects.reshape(-1, 3) @ rotation.T + tvec.reshape(1, 3)
        if np.min(camera_points[:, 2]) <= 0:
            continue
        projected = cv2.projectPoints(objects, rvec, tvec, k, np.zeros(5))[0]
        error = float(
            np.sqrt(
                np.mean(
                    np.sum(
                        (projected.reshape(-1, 2) - pixels.reshape(-1, 2)) ** 2, axis=1
                    )
                )
            )
        )
        transform = np.eye(4)
        transform[:3, :3], transform[:3, 3] = rotation, tvec.ravel()
        candidates.append((error, transform))
    candidates.sort(key=lambda item: item[0])
    if not candidates:
        return "pnp_failed", details, None
    details["reprojection_px"] = candidates[0][0]
    if candidates[0][0] > MAX_REPROJECTION_PX:
        return "high_reprojection", details, None
    if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < MIN_PLANAR_GAP_PX:
        return "planar_ambiguity", details, None
    return "valid", details, np.linalg.inv(candidates[0][1])


def sample_frames(
    reader: AnyReader,
    config: dict,
    k: np.ndarray,
    size: tuple[int, int],
    poses: list[PoseSample],
) -> list[dict]:
    camera = config["camera"]
    board = make_board(config)
    params = cv2.aruco.CharucoParameters()
    params.cameraMatrix, params.distCoeffs = k, np.zeros(5)
    detector = cv2.aruco.CharucoDetector(board, params)
    extrinsic = rigid(camera["pose_from_rectified_image"])
    stamps = np.array([item.stamp_ns for item in poses], dtype=np.int64)
    intervals = windows(config)
    rows = []
    first, due = None, 0
    selected = [
        item for item in reader.connections if item.topic == camera["image_topic"]
    ]
    for connection, record_ns, raw in reader.messages(connections=selected):
        message = reader.deserialize(raw, connection.msgtype)
        stamp = header_stamp_ns(message)
        if first is None:
            first = stamp
        if stamp < due:
            continue
        due = first + ((stamp - first) // PERIOD_NS + 1) * PERIOD_NS
        elapsed = (stamp - first) / 1e9
        segment = next(
            (name for name, start, end in intervals if start <= elapsed < end),
            "outside",
        )
        row = {
            "source_ns": stamp,
            "record_ns": record_ns,
            "elapsed_s": elapsed,
            "segment": segment,
            "status": "outside",
        }
        rows.append(row)
        if segment == "outside":
            continue
        image = decode_color_image(message, connection.msgtype)
        if image is None:
            row["status"] = "decode_failed"
            continue
        if image.shape[:2] != size:
            raise ValueError("decoded image dimensions differ from CameraInfo")
        status, details, reference = board_pose(image, board, detector, k)
        row.update(details, status=status)
        if status != "valid":
            continue
        upper = int(np.searchsorted(stamps, stamp))
        if upper < len(stamps) and stamps[upper] == stamp:
            pose = poses[upper]
        elif (
            upper == 0
            or upper == len(stamps)
            or stamps[upper] - stamps[upper - 1] > MAX_POSE_GAP_NS
        ):
            row["status"] = "pose_unavailable"
            continue
        else:
            pose = interpolate_pose(poses[upper - 1], poses[upper], stamp)
        row["reference"] = reference
        row["estimate"] = matrix_from_pose(pose) @ extrinsic
    return rows


def average_transform(transforms: list[np.ndarray]) -> np.ndarray:
    average = np.eye(4)
    u, _, vt = np.linalg.svd(np.mean([item[:3, :3] for item in transforms], axis=0))
    average[:3, :3] = u @ np.diag([1, 1, np.linalg.det(u @ vt)]) @ vt
    average[:3, 3] = np.mean([item[:3, 3] for item in transforms], axis=0)
    return average


def error_pair(reference: np.ndarray, estimate: np.ndarray) -> tuple[float, float]:
    difference = np.linalg.inv(reference) @ estimate
    return float(np.linalg.norm(difference[:3, 3]) * 1000), rotation_distance_deg(
        np.eye(4), difference
    )


def statistics(values: list[float]) -> dict | None:
    if not values:
        return None
    array = np.asarray(values)
    return {
        "rmse": float(np.sqrt(np.mean(array**2))),
        "median": float(np.median(array)),
        "p95": float(np.percentile(array, 95)),
        "max": float(np.max(array)),
    }


def score(rows: list[dict], config: dict, inventory: dict) -> dict:
    issues = []
    reference = [
        row
        for row in rows
        if row["segment"] == "reference" and row["status"] == "valid"
    ]
    alignment = None
    reference_scatter = None
    if len(reference) < 15:
        issues.append("reference requires >= 15 valid pairs")
    else:
        centre = average_transform([row["reference"] for row in reference])
        scatter = [error_pair(centre, row["reference"]) for row in reference]
        reference_scatter = {
            "position_mm": statistics([item[0] for item in scatter]),
            "rotation_deg": statistics([item[1] for item in scatter]),
        }
        if (
            reference_scatter["position_mm"]["max"] > 10
            or reference_scatter["rotation_deg"]["max"] > 2
        ):
            issues.append(
                "reference is not stationary within 10 mm / 2 deg; secure camera and board"
            )
        alignment = average_transform(
            [row["reference"] @ np.linalg.inv(row["estimate"]) for row in reference]
        )
        for row in rows:
            if row["status"] == "valid":
                row["estimate"] = alignment @ row["estimate"]
                row["position_mm"], row["rotation_deg"] = error_pair(
                    row["reference"], row["estimate"]
                )
    image_range = inventory["source_header_ranges_ns"][config["camera"]["image_topic"]]
    duration = (image_range[1] - image_range[0]) / 1e9
    segments = {}
    for interval in config["evaluation_intervals"]:
        name = interval["name"]
        selected = [row for row in rows if row["segment"] == name]
        valid = [row for row in selected if row["status"] == "valid"]
        expected = math.ceil(interval["end_s"] * 1e9 / PERIOD_NS) - math.ceil(
            interval["start_s"] * 1e9 / PERIOD_NS
        )
        fraction = len(valid) / max(expected, len(selected), 1)
        if len(valid) < 20 or duration < interval["end_s"] - PERIOD_NS / 1e9:
            issues.append(
                f"{name}: require >= 20 valid pairs and the complete declared interval"
            )
        # Pair only within an unbroken valid run, never across a board/pose outage.
        rpe = []
        run = []
        for row in selected:
            if row["status"] != "valid":
                run = []
                continue
            if run and row["source_ns"] - run[-1]["source_ns"] > PERIOD_NS * 1.5:
                run = []
            target = row["source_ns"] - 1_000_000_000
            earlier = min(
                run, key=lambda item: abs(item["source_ns"] - target), default=None
            )
            if (
                earlier is not None
                and abs(earlier["source_ns"] - target) <= MAX_POSE_GAP_NS
            ):
                rpe.append(
                    error_pair(
                        np.linalg.inv(earlier["reference"]) @ row["reference"],
                        np.linalg.inv(earlier["estimate"]) @ row["estimate"],
                    )
                )
            run.append(row)
            run = [
                item for item in run if item["source_ns"] >= target - MAX_POSE_GAP_NS
            ]
        if not rpe:
            issues.append(f"{name}: no continuous 1-second RPE pairs")
        metrics = {
            "sampled": len(selected),
            "expected_samples": expected,
            "valid": len(valid),
            "valid_fraction": fraction,
            "rejections": dict(
                Counter(row["status"] for row in selected if row["status"] != "valid")
            ),
            "missing_image_samples": max(0, expected - len(selected)),
            "position_mm": statistics([row["position_mm"] for row in valid])
            if alignment is not None
            else None,
            "rotation_deg": statistics([row["rotation_deg"] for row in valid])
            if alignment is not None
            else None,
            "rpe_1s_pairs": len(rpe),
            "rpe_1s_position_mm": statistics([item[0] for item in rpe]),
            "rpe_1s_rotation_deg": statistics([item[1] for item in rpe]),
            "acceptance": "NOT_SPECIFIED",
        }
        acceptance = config["acceptance"]
        if acceptance is not None and metrics["position_mm"] is not None:
            passed = (
                metrics["position_mm"]["p95"] <= acceptance["position_p95_mm"]
                and metrics["rotation_deg"]["p95"] <= acceptance["rotation_p95_deg"]
                and fraction >= acceptance["min_valid_fraction"]
            )
            metrics["acceptance"] = "PASS" if passed else "FAIL"
        segments[name] = metrics
    state = (
        "INVALID"
        if issues
        else "FAIL"
        if any(item["acceptance"] == "FAIL" for item in segments.values())
        else "PASS"
        if config["acceptance"]
        else "MEASURED_NO_THRESHOLDS"
    )
    if issues:
        for metrics in segments.values():
            metrics["acceptance"] = "INVALID"
    return {
        "status": state,
        "issues": issues,
        "alignment": "initial-window SE3, scale fixed at 1, no time-offset fitting",
        "board_from_map": None if alignment is None else alignment.tolist(),
        "reference_valid": len(reference),
        "reference_scatter": reference_scatter,
        "segments": segments,
    }


def write_results(output: Path, rows: list[dict], summary: dict) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    fields = [
        "source_ns",
        "record_ns",
        "elapsed_s",
        "segment",
        "status",
        "corners",
        "reprojection_px",
        "position_mm",
        "rotation_deg",
    ]
    with (output / "samples.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    plt = pyplot()
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    for interval in summary["config"]["evaluation_intervals"]:
        selected = [row for row in rows if row["segment"] == interval["name"]]
        for axis, key, label in zip(
            axes,
            ("position_mm", "rotation_deg"),
            ("Position error (mm)", "Rotation error (deg)"),
        ):
            axis.plot(
                [row["elapsed_s"] for row in selected],
                [row.get(key, float("nan")) for row in selected],
                ".-",
                markersize=2,
                label=interval["name"],
            )
            axis.set_ylabel(label)
            axis.grid(alpha=0.3)
    axes[0].legend()
    axes[1].set_xlabel("Seconds from first source image header")
    fig.suptitle(f"Fixed-board reference / initial SE3 only / {summary['status']}")
    fig.tight_layout()
    fig.savefig(output / "errors.png", dpi=160)
    plt.close(fig)
    lines = [
        "# 标定板参照精度报告",
        "",
        f"结果：`{summary['status']}`。起始参考段对齐一次，尺度固定为 1；仅覆盖板可见并通过质量门限的采样。",
        "",
        "此参照与被测系统共用图像和相机参数，不是独立动捕真值，也不衡量整幅地图点云精度。",
        "",
        "| 片段 | 有效/计划 | 位置 P95 (mm) | 角度 P95 (deg) | 1 s RPE 位置 RMSE (mm) | 判定 |",
        "|---|---:|---:|---:|---:|---|",
    ]

    def metric(value: dict | None, key: str) -> str:
        return "不可用" if value is None else f"{value[key]:.3f}"

    for name, item in summary["segments"].items():
        lines.append(
            f"| {name} | {item['valid']}/{item['expected_samples']} | {metric(item['position_mm'], 'p95')} | {metric(item['rotation_deg'], 'p95')} | {metric(item['rpe_1s_position_mm'], 'rmse')} | {item['acceptance']} |"
        )
    lines += [
        "",
        "![误差曲线](errors.png)",
        "",
        "数据问题：" + ("；".join(summary["issues"]) or "无"),
        "",
        "完整统计、质量门限、输入指纹和配置见 summary.json；逐帧失败原因见 samples.csv。采集与方法依据见 docs/BOARD_ACCURACY_PROTOCOL.md。",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("make-board", "inspect", "evaluate"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bag", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, evaluation=args.command == "evaluate")
        if args.command == "make-board":
            if args.output is None:
                raise ValueError("make-board requires --output (new directory)")
            print_board(config, args.output)
            print(args.output / "board_A3.pdf")
            return 0
        if args.bag is None:
            raise ValueError("inspect/evaluate requires --bag")
        if args.command == "evaluate" and (
            args.output is None
            or args.output.exists()
            or args.output.resolve().is_relative_to(args.bag.resolve())
        ):
            raise ValueError(
                "evaluate requires --output outside the source bag, in a new directory"
            )
        with reader_for(args.bag) as reader:
            k, size, poses, inventory = scan(reader, config)
            if args.command == "inspect":
                print(json.dumps(inventory, indent=2, ensure_ascii=False))
                return 0
            rows = sample_frames(reader, config, k, size, poses)
        summary = score(rows, config, inventory)
        summary.update(
            {
                "schema_version": 1,
                "config": config,
                "inventory": inventory,
                "quality_gates": {
                    "sample_hz": 5,
                    "pose_bracket_max_ms": 50,
                    "corners_min": MIN_CORNERS,
                    "hull_image_fraction_min": 0.002,
                    "reprojection_max_px": MAX_REPROJECTION_PX,
                    "planar_candidate_gap_min_px": MIN_PLANAR_GAP_PX,
                },
                "source_bag": str(args.bag.resolve()),
                "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
                "implementation_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "metadata_sha256": hashlib.sha256(
                    (args.bag / "metadata.yaml").read_bytes()
                ).hexdigest(),
                "source_files": [
                    {
                        "name": item.name,
                        "bytes": item.stat().st_size,
                        "mtime_ns": item.stat().st_mtime_ns,
                    }
                    for item in sorted(args.bag.iterdir())
                    if item.is_file()
                ],
                "versions": {"opencv": cv2.__version__, "numpy": np.__version__},
            }
        )
        write_results(args.output, rows, summary)
        print(
            json.dumps(
                {
                    "status": summary["status"],
                    "issues": summary["issues"],
                    "report": str(args.output / "report.md"),
                },
                ensure_ascii=False,
            )
        )
        return (
            2
            if summary["status"] == "INVALID"
            else 3
            if summary["status"] == "FAIL"
            else 0
        )
    except (ValueError, KeyError, OSError, cv2.error) as error:
        print(f"board accuracy: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
