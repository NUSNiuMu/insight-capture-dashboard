"""Move sessions recorded before gravity alignment into GRAVITY_FRAME.

The raw recording is never modified: session.json gains the rotation that
maps its stored live poses into the gravity-aligned frame (applied when the
poses are read), and its reference is replaced so an offline recompute
produces that frame directly. The original reference is kept for revert().

    python3 -m insight_capture.aruco_capture.frame_migration [--revert] SESSION...
"""

import json
import sys
from pathlib import Path

import numpy as np

from .capture import write_json
from .pose import GRAVITY_FRAME, gravity_frame
from .pose_track import OFFLINE_DB


def convert(path):
    """Return False if the session is already in the gravity frame."""
    meta_path = Path(path) / "session.json"
    meta = json.loads(meta_path.read_text())
    if "reference_frame" in meta:
        return False
    if meta["status"] != "stopped":
        raise ValueError("录制未停止，不能转换")
    if not meta["config"].get("vio_enabled", True):
        raise ValueError("关闭 VIO 的旧录制没有记录重力方向，无法转换")
    if meta.get("reference_from_vio_world") is None or meta.get("imu_from_rgb") is None:
        raise ValueError("录制缺少参考系或 IMU–RGB 外参，无法转换")
    if (Path(path) / OFFLINE_DB).exists():
        raise ValueError(f"旧坐标系的 {OFFLINE_DB} 会与转换后的参考系不一致，请先删除后再转换")
    reference = np.array(meta["reference_from_vio_world"], float)
    imu_from_rgb = np.array(meta["imu_from_rgb"], float)
    # The legacy reference is inverse(T_world_IMU_start); Insight9's VIO
    # world has z up, so this is the IMU-frame up direction at the start.
    correction = gravity_frame(reference[:3, :3] @ [0.0, 0.0, 1.0], imu_from_rgb[:3, 2])
    meta.update(
        legacy_reference_from_vio_world=meta["reference_from_vio_world"],
        live_pose_correction=correction.tolist(),
        reference_from_vio_world=(correction @ reference).tolist(),
        reference_from_rgb=(correction @ imu_from_rgb).tolist(),
        reference_frame=GRAVITY_FRAME,
    )
    write_json(meta_path, meta)
    return True


def revert(path):
    """Return False if the session was not converted by convert()."""
    meta_path = Path(path) / "session.json"
    meta = json.loads(meta_path.read_text())
    if "legacy_reference_from_vio_world" not in meta:
        return False
    if (Path(path) / OFFLINE_DB).exists():
        raise ValueError(f"重力坐标系的 {OFFLINE_DB} 会与还原后的参考系不一致，请先删除后再还原")
    meta["reference_from_vio_world"] = meta.pop("legacy_reference_from_vio_world")
    for key in ("live_pose_correction", "reference_from_rgb", "reference_frame"):
        meta.pop(key)
    write_json(meta_path, meta)
    return True


def main(arguments):
    action = revert if arguments[:1] == ["--revert"] else convert
    failed = False
    for path in arguments[action is revert :]:
        try:
            changed = action(Path(path))
            print(f"{path}: {'done' if changed else 'skipped (nothing to do)'}")
        except (ValueError, OSError, KeyError) as exc:
            failed = True
            print(f"{path}: FAILED {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
