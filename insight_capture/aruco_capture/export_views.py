"""Image streams a LeRobot export can contain, and how each frame is made.

Every view reads one recorded stream and transforms it; the user picks any
subset on the 处理与导出 page and may rename each video key. Without a
selection the export keeps the original behaviour: every recorded stream at
its native resolution.
"""

import json
import re

import cv2
import numpy as np

from .pose_track import _vio_lookup

KEY = re.compile(r"[A-Za-z0-9_.-]+")
# Physical width a hand crop covers, so the gripper keeps the same size in
# the crop however near or far it is from the head camera.
HAND_CROP_M = 0.25


def catalog(rgb_streams):
    """All selectable views for recordings with these RGB stream names."""
    views = [
        {"id": "head_pad448", "key": "head_view", "label": "头部 · 补黑边 448×448", "group": "头部相机",
         "size": [448, 448], "default": True,
         "description": "Insight9 画面上下补黑边成正方形，等比缩放到 448"},
        {"id": "head_pad224", "key": "head_view_224", "label": "头部 · 补黑边 224×224", "group": "头部相机",
         "size": [224, 224], "default": False, "description": "同上，缩放到 224"},
        {"id": "subtask_end_head", "key": "subtask_end_head_view", "label": "子任务结束帧 · 补黑边 224×224",
         "group": "头部相机", "size": [224, 224], "default": True,
         "description": "当前片段（子任务）结束时刻的头部画面，整段片段内保持不变，作为目标图像"},
        {"id": "head_raw", "key": "head", "label": "头部 · 原始分辨率", "group": "头部相机",
         "size": None, "default": False, "description": "1920×1088 原图（旧版导出的格式）"},
        {"id": "hand_crop_left", "key": "head_left_hand_crop", "label": "左手高清裁剪 224×224",
         "group": "手部裁剪（候选）", "size": [224, 224], "default": False,
         "description": "按位姿与标定把左手夹爪（cube 与夹指之间）投影到头部画面，裁出约 25 cm 见方的区域后缩放；位姿无效时沿用上一位置"},
        {"id": "hand_crop_right", "key": "head_right_hand_crop", "label": "右手高清裁剪 224×224",
         "group": "手部裁剪（候选）", "size": [224, 224], "default": False,
         "description": "同上，右手 cube"},
    ]
    for name in rgb_streams:
        views.append({"id": f"rgb_crop224:{name}", "key": name, "label": f"{name} · 中心裁剪 224×224",
                      "group": "手腕 RGB", "size": [224, 224], "default": True,
                      "description": "1280×720 中心裁出 720×720 正方形后缩放，不填充"})
        views.append({"id": f"rgb_raw:{name}", "key": f"{name}_raw", "label": f"{name} · 原始分辨率",
                      "group": "手腕 RGB", "size": None, "default": False,
                      "description": "原图（旧版导出的格式）"})
    return views


def default_selection(video_names):
    """Original behaviour: every recorded stream, native resolution, own name."""
    return [{"id": "head_raw" if n == "head" else f"rgb_raw:{n}", "key": n} for n in video_names]


def validate_selection(selection, video_names):
    if not isinstance(selection, list) or not selection:
        raise ValueError("请至少选择一路导出图像")
    known = {v["id"] for v in catalog([n for n in video_names if n != "head"])}
    keys, result = set(), []
    for item in selection:
        view, key = str(item.get("id", "")), str(item.get("key", "")).strip()
        if view not in known:
            raise ValueError(f"这条录制没有可导出的图像 {view}")
        if not KEY.fullmatch(key):
            raise ValueError(f"视频名称 {key!r} 只能用英文字母、数字和 _ . -")
        if key in keys:
            raise ValueError(f"视频名称 {key} 重复")
        keys.add(key)
        result.append({"id": view, "key": key})
    return result


def pad_square(image, side):
    h, w = image.shape[:2]
    n = max(h, w)
    top, left = (n - h) // 2, (n - w) // 2
    image = cv2.copyMakeBorder(image, top, n - h - top, left, n - w - left, cv2.BORDER_CONSTANT, value=0)
    return cv2.resize(image, (side, side), interpolation=cv2.INTER_AREA)


def center_crop(image, side):
    h, w = image.shape[:2]
    n = min(h, w)
    y, x = (h - n) // 2, (w - n) // 2
    return cv2.resize(image[y:y + n, x:x + n], (side, side), interpolation=cv2.INTER_AREA)


class Renderer:
    """Makes one exported frame of a view for grid index ``i`` of a session."""

    def __init__(self, path, meta, db, video, grid, state, hand_crop_m=HAND_CROP_M):
        self.db, self.video, self.grid, self.state = db, video, grid, state
        self.hand_crop_m = float(hand_crop_m)
        self.cache = {}
        self.last_center = {}
        calibration = meta.get("calibration") or {}
        self.intrinsic = np.array(calibration["intrinsic"], float) if calibration else None
        self.native = (int(calibration.get("width", 0)), int(calibration.get("height", 0)))
        self.imu_from_rgb = np.array(meta["imu_from_rgb"], float) if meta.get("imu_from_rgb") else None
        self.reference = np.array(meta.get("reference_from_vio_world", np.eye(4)), float)
        self.vio = _vio_lookup(db) if meta["config"].get("vio_enabled", True) else None
        # Crops centre on the marker cube, whose projection lands exactly on
        # it; the configured TCP sits ~10 cm behind the cube (on the handle),
        # so centring on it cut the jaws off the top of the crop.
        from .pose import DirectPose

        pose = DirectPose(meta["config"], "none")
        self.tcp_from_cube = {}
        for side in ("left", "right"):
            cube_from_tcp = pose.config.targets[f"gripper_{side}"].cube_from_camera_center @ pose.tcp[f"gripper_{side}"]
            # Aim halfway from the cube towards the jaws (the TCP's sideways
            # offset), so the crop holds both the cube and the fingers.
            aim = np.eye(4)
            aim[0, 3] = cube_from_tcp[0, 3] / 2
            self.tcp_from_cube[side] = np.linalg.inv(cube_from_tcp) @ aim

    def _decode(self, rowid):
        if rowid not in self.cache:
            if len(self.cache) > 16:
                self.cache.clear()
            payload, stamp = self.db.execute(
                "SELECT payload, stamp_ns FROM samples WHERE rowid=?", (int(rowid),)
            ).fetchone()
            bgr = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise ValueError("无法解码已录制图像")
            self.cache[rowid] = (cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), stamp)
        return self.cache[rowid]

    def _stream(self, name, i):
        return self._decode(self.video[name][0][i])

    def frame(self, view, i, label):
        kind, _, stream = view.partition(":")
        if kind == "head_raw":
            return self._stream("head", i)[0]
        if kind == "head_pad448":
            return pad_square(self._stream("head", i)[0], 448)
        if kind == "head_pad224":
            return pad_square(self._stream("head", i)[0], 224)
        if kind == "subtask_end_head":
            return pad_square(self._stream("head", self._end_index(label))[0], 224)
        if kind == "rgb_raw":
            return self._stream(stream, i)[0]
        if kind == "rgb_crop224":
            return center_crop(self._stream(stream, i)[0], 224)
        if kind in ("hand_crop_left", "hand_crop_right"):
            return self._hand_crop(kind.rsplit("_", 1)[1], i)
        raise ValueError(f"未知导出图像 {view}")

    def _end_index(self, label):
        """Last grid index of the segment that has a head frame."""
        rowids, found = self.video["head"]
        j = int(np.searchsorted(self.grid, label["end_s"], side="left")) - 1
        start = int(np.searchsorted(self.grid, label["start_s"], side="left"))
        for k in range(min(j, len(self.grid) - 1), start - 1, -1):
            if found[k]:
                return k
        return start

    def project(self, side, i, stamp):
        """Pixel of the side's TCP in the recorded (display-rotated) head image."""
        if self.intrinsic is None or self.imu_from_rgb is None:
            return None
        if self.vio is not None:
            head = self.vio(int(stamp)) if stamp else None
            if head is None:
                return None
            from insight_capture.runtime.mapping.geometry import matrix_from_pose
            world_from_imu = matrix_from_pose(head)
        else:
            world_from_imu = np.eye(4)
        reference_from_rgb = self.reference @ world_from_imu @ self.imu_from_rgb
        offset = 0 if side == "left" else 10
        rows = self.state[i, offset + 3:offset + 9].reshape(2, 3).astype(float)
        reference_from_tcp = np.eye(4)
        reference_from_tcp[:3, :3] = np.vstack([rows, np.cross(rows[0], rows[1])])
        reference_from_tcp[:3, 3] = self.state[i, offset:offset + 3]
        point = (reference_from_tcp @ self.tcp_from_cube[side])[:, 3]
        x, y, z = (np.linalg.inv(reference_from_rgb) @ point)[:3]
        if z <= 0.05:
            return None
        u = self.intrinsic[0, 0] * x / z + self.intrinsic[0, 2]
        v = self.intrinsic[1, 1] * y / z + self.intrinsic[1, 2]
        return u, v, self.intrinsic[0, 0] * self.hand_crop_m / z

    def _hand_crop(self, side, i):
        image, stamp = self._stream("head", i)
        h, w = image.shape[:2]
        pixel = self.project(side, i, stamp)
        if pixel is not None:
            u, v, side_px = pixel
            native_w, native_h = self.native
            if (w, h) == (native_h, native_w) and native_w != native_h:
                # Recorded copy is the native image rotated 90° clockwise.
                u, v = native_h - 1 - v, u
            if -w < u < 2 * w and -h < v < 2 * h:
                self.last_center[side] = (u, v, side_px)
        u, v, side_px = self.last_center.get(side, (w / 2, h / 2, 480))
        n = int(np.clip(side_px, 160, min(h, w)))
        x0 = int(np.clip(round(u - n / 2), 0, w - n))
        y0 = int(np.clip(round(v - n / 2), 0, h - n))
        return cv2.resize(image[y0:y0 + n, x0:x0 + n], (224, 224), interpolation=cv2.INTER_AREA)


def describe_fields():
    """Non-image columns every export writes, for the export page's list."""
    return [
        {"key": "observation.state", "label": "双臂状态 20 维：每只手 TCP 位置 3 + 旋转 6D 6 + 夹爪开合 1"},
        {"key": "action", "label": "动作：下一帧的绝对状态（20 维）"},
        {"key": "timestamp", "label": "片段内时间（s）"},
        {"key": "source_time_s", "label": "在原录制中的时间（s）"},
        {"key": "frame_index / episode_index / index", "label": "帧序号、episode 序号、全局序号"},
        {"key": "task_index", "label": "任务文本索引（meta/tasks.parquet）"},
        {"key": "valid.frame", "label": "有效帧标记"},
        {"key": "episode: tasks / subtask / task_id / subtask_id / participating_hands", "label": "片段标注元数据"},
        {"key": "meta/stats.json · modality.json · manifest.json", "label": "统计量、模态说明、来源清单"},
    ]
