"""Background pre-annotation of a stopped recording.

1. 静止分段（双臂同时静止 >= 2 s）切出 task 级的动作段，复位现场的停顿丢弃。
2. VLM 服务（insight_capture.vlm_service）看全部动作段，认出整条录制的
   task（导入了动作表时从表中选），并写一句改写的英文说法。
3. 每个动作段按 4 帧/秒送给服务，由模型切出带起止时间的 subtask；超过
   45 s 的动作段再按 40 s 窗口（重叠 5 s）切开。
4. 用位姿修正：边界吸附到最近的运动起止点；左右前后上下与夹爪开合这类
   subtask 用位移方向和开合度变化校验，不符的标为存疑；参与手臂按位姿判断。

结果写入录制目录的 segments_draft.json；segments.json 只在人工保存时写入。
"""

import base64
from contextlib import closing
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

import cv2
import numpy as np

from .capture import write_json
from .dataset import align, arm_stationary, connect, suggest_pause_segments

DRAFT = "segments_draft.json"
# One arm counts as idle when it moves for less than this share of the other
# arm's moving time (the free hand still drifts a little while holding still).
HANDS_RATIO = 0.3
FRAME_MAX_SIDE = 448
TASK_FPS = 1.0
TASK_MAX_FRAMES = 200
SEGMENT_FPS = 4.0
MAX_WINDOW_S = 45.0
WINDOW_S = 40.0
WINDOW_OVERLAP_S = 5.0
SNAP_S = 0.75
MIN_DRAFT_S = 0.3
# Subtask ids whose meaning the pose can check, in the gravity-aligned frame
# (+x right, +y forward, +z up).
DIRECTIONS = {
    "move_right": (0, 1), "move_left": (0, -1), "move_forward": (1, 1),
    "move_backward": (1, -1), "move_up": (2, 1), "move_down": (2, -1),
}
DIRECTION_ZH = {v: k for k, v in {
    "向右": (0, 1), "向左": (0, -1), "向前": (1, 1), "向后": (1, -1), "向上": (2, 1), "向下": (2, -1),
}.items()}
GRIPPER = {"close_gripper": -1, "open_gripper": 1}
ARMS = {"left": (0,), "right": (10,), "both": (0, 10)}


SETTINGS = "_annotation.json"


def normalize_url(value):
    """Canonical service URL, '' to disable; accepts a bare host:port."""
    value = str(value or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parts = urllib.parse.urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = None
        valid = False
    else:
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    if not valid or parts.query or parts.fragment:
        raise ValueError("推理服务地址格式应为 http://主机:端口，例如 http://10.198.1.220:8780")
    return f"{parts.scheme}://{parts.hostname}" + (f":{port}" if port else "") + parts.path


def load_url(root, default=""):
    """The URL saved from the page, else `default` (ARUCO_ANNOTATION_URL)."""
    path = root / SETTINGS
    if path.is_file():
        return normalize_url(json.loads(path.read_text()).get("url", ""))
    return normalize_url(default)


def save_url(root, url):
    url = normalize_url(url)
    write_json(root / SETTINGS, {"url": url})
    return url


def probe(url, timeout=3.0):
    """The service's /health reply, or None when unset or unreachable."""
    if not url:
        return None
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            value = json.load(r)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("status") == "ok" else None


def segment_hands(moving, first, last):
    """'left', 'right' or 'both' from per-arm moving masks over intervals [first, last)."""
    share = {side: float(mask[first:last].mean()) if last > first else 0.0 for side, mask in moving.items()}
    busiest = max(share, key=share.get)
    other = "right" if busiest == "left" else "left"
    if share[busiest] < 0.05 or share[other] >= HANDS_RATIO * share[busiest]:
        return "both"
    return busiest


def sample_times(start, end, fps):
    """Evenly spaced frame times in [start, end), an even count (Qwen video
    patches span two frames); the effective rate is count / (end - start)."""
    count = max(2, int((end - start) * fps) // 2 * 2)
    return start + (np.arange(count) + 0.5) * (end - start) / count


def windows(start, end):
    if end - start <= MAX_WINDOW_S:
        return [(start, end)]
    found, w0 = [], start
    while True:
        w1 = min(end, w0 + WINDOW_S)
        found.append((w0, w1))
        if w1 >= end:
            return found
        w0 = w1 - WINDOW_OVERLAP_S


class HeadFrames:
    """Nearest head-camera JPEGs from the session database, downscaled."""

    def __init__(self, db):
        self.db = db
        self.stamps = np.array(
            [r[0] for r in db.execute("SELECT t FROM samples WHERE kind='image' AND name='head' ORDER BY t")]
        )
        if not len(self.stamps):
            raise ValueError("录制中没有观察相机画面，无法预标注")

    def encoded(self, times):
        frames = []
        for t in times:
            i = int(np.clip(np.searchsorted(self.stamps, t), 0, len(self.stamps) - 1))
            if i and abs(self.stamps[i - 1] - t) < abs(self.stamps[i] - t):
                i -= 1
            payload = self.db.execute(
                "SELECT payload FROM samples WHERE kind='image' AND name='head' AND t=?",
                (float(self.stamps[i]),),
            ).fetchone()[0]
            image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue
            h, w = image.shape[:2]
            scale = min(1.0, FRAME_MAX_SIDE / max(h, w))
            if scale < 1:
                image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
            ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                frames.append(base64.b64encode(buffer.tobytes()).decode())
        return frames


class ServiceDown(RuntimeError):
    pass


def request_json(url, route, body, timeout=600):
    request = urllib.request.Request(
        url.rstrip("/") + route,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:  # the service answered: flag this part only
        try:
            reason = json.load(exc).get("error", "")
        except ValueError:
            reason = ""
        return {"flags": [f"推理服务报错 {exc.code}：{reason}".rstrip("：")]}
    except (OSError, ValueError) as exc:
        raise ServiceDown(f"推理服务连接中断：{exc}") from exc


def motion_edges(grid, active):
    """Times where motion starts and stops, from a per-interval active mask."""
    padded = np.concatenate(([False], active, [False])).astype(np.int8)
    runs = np.flatnonzero(np.diff(padded)).reshape(-1, 2)
    return grid[runs[:, 0]], grid[np.minimum(runs[:, 1], len(grid) - 1)]


def snap(t, candidates, lo, hi):
    near = candidates[(np.abs(candidates - t) <= SNAP_S) & (candidates >= lo) & (candidates <= hi)]
    return float(near[np.argmin(np.abs(near - t))]) if len(near) else t


def _despike(values, size=5):
    """Running median along time, so a single-frame pose jump cannot dominate."""
    if len(values) < size:
        return values
    half = size // 2
    padded = np.concatenate([values[:1].repeat(half, 0), values, values[-1:].repeat(half, 0)])
    return np.median(np.stack([padded[i : i + len(values)] for i in range(size)]), axis=0)


def pose_check(subtask_id, state, good, first, last, hands, gravity):
    """Flags where the pose contradicts a direction / gripper subtask."""
    if subtask_id not in DIRECTIONS and subtask_id not in GRIPPER:
        return []
    offsets = ARMS.get(hands, ARMS["both"])
    frames = np.arange(first, last + 1)[good[first : last + 1]]
    if len(frames) < 3:
        return []
    window = _despike(state[frames])
    if subtask_id in DIRECTIONS and gravity:
        # The largest excursion from the start: out-and-back still shows.
        moves = [window[:, o : o + 3] - window[0, o : o + 3] for o in offsets]
        excursions = [m[np.argmax(np.linalg.norm(m, axis=1))] for m in moves]
        d = max(excursions, key=np.linalg.norm)
        if np.linalg.norm(d) < 0.03:
            return ["位姿显示这一段夹爪几乎没有移动"]
        axis = int(np.argmax(np.abs(d)))
        found = (axis, 1 if d[axis] > 0 else -1)
        if found != DIRECTIONS[subtask_id]:
            return [f"位姿显示主要{DIRECTION_ZH[found]}移动 {abs(d[axis]) * 100:.0f} cm，与 {subtask_id} 不符"]
    if subtask_id in GRIPPER:
        sign = GRIPPER[subtask_id]
        change = max(sign * (np.max(sign * window[:, o + 9]) * sign - window[0, o + 9]) for o in offsets)
        if change < 0.02:
            return ["位姿未见夹爪" + ("闭合" if sign < 0 else "张开")]
    return []


OTHER = "other"
UNSURE_FLAG = "不确定或无法标注，标为 other"


def cover(start, end, items, min_s):
    """Tile [start, end] completely with ``items`` (dicts with "start"/"end",
    sorted, non-overlapping): gaps of at least ``min_s`` become ``None``
    placeholders, shorter ones are absorbed by the neighbouring item."""
    out, cursor = [], start
    for item in items:
        a, b = max(item["start"], cursor), min(item["end"], end)
        if b - a < min_s:
            continue
        if a - cursor >= min_s:
            out.append((cursor, a, None))
        elif out and out[-1][2] is not None:
            out[-1] = (out[-1][0], a, out[-1][2])   # tiny gap: extend previous
        else:
            a = cursor                               # tiny gap at the start
        out.append((a, b, item))
        cursor = b
    if end - cursor >= min_s or not out:
        out.append((cursor, end, None))
    else:
        out[-1] = (out[-1][0], end, out[-1][2])
    return out


def run(path, url, table=None, model="", progress=lambda done, total: None, segmentation=None):
    """Pre-annotate one stopped session and write its draft; returns the draft."""
    started = time.monotonic()
    meta, grid, state, video, good, report, _reasons = align(path)
    fps, duration = report["fps"], float(meta["duration_s"])
    stretches = [
        (s["start_s"], s["end_s"])
        for s in suggest_pause_segments(grid, state, good, fps, duration_s=duration, **(segmentation or {}))
    ]
    pair_good = good[:-1] & good[1:]
    moving = {
        side: pair_good & ~arm_stationary(state, fps, offset)
        for side, offset in (("left", 0), ("right", 10))
    }
    onsets, offsets = motion_edges(grid, moving["left"] | moving["right"])
    gravity = "reference_frame" in meta
    table = table if table and table.get("tasks") else None
    plan = [(stretch, windows(*stretch)) for stretch in stretches]
    total, done = 1 + sum(len(w) for _, w in plan), 0
    progress(done, total)
    task = {"task_id": "", "task": "", "description": "", "flags": []}
    drafts = []
    with closing(connect(path)) as db:
        frames = HeadFrames(db)
        if stretches:
            # 1. Which task the recording performs, from its motion only.
            active_s = sum(b - a for a, b in stretches)
            rate = min(TASK_FPS, TASK_MAX_FRAMES / active_s)
            times = np.concatenate([sample_times(a, b, rate) for a, b in stretches])
            task = {**task, **request_json(url, "/v1/task", {
                "frames": frames.encoded(times), "fps": rate, "action_table": table,
            })}
        done += 1
        progress(done, total)
        task_flags = list(task.get("flags", []))
        if table and not task.get("task_id"):
            task_flags.append("未确定 task，subtask 未关联动作表")

        # 2. Subtasks with times, per stretch of motion.
        for (start, end), parts in plan:
            found, failures = [], []
            for w0, w1 in parts:
                times = sample_times(w0, w1, SEGMENT_FPS)
                encoded = frames.encoded(times)
                answer = request_json(url, "/v1/segments", {
                    "frames": encoded, "fps": len(times) / (w1 - w0), "action_table": table,
                    "task_id": task.get("task_id", ""), "task": task.get("task", ""),
                }) if len(encoded) >= 2 else {"flags": ["可用画面不足，未送模型"]}
                # Keep what lies in this window's half of each overlap.
                lo = w0 + (WINDOW_OVERLAP_S / 2 if w0 > start else 0)
                hi = w1 - (WINDOW_OVERLAP_S / 2 if w1 < end else 0)
                for item in answer.get("segments", []):
                    a, b = w0 + item["start"], w0 + item["end"]
                    if lo <= (a + b) / 2 < hi:
                        found.append({**item, "start": a, "end": b})
                if not answer.get("segments"):
                    failures += answer.get("flags", [])
                done += 1
                progress(done, total)
            if not found:
                found = [{"start": start, "end": end, "subtask_id": "", "subtask": "",
                          "flags": ["模型未切分出子任务，整段保留"] + failures}]
            # 3. Snap to motion onsets / offsets, keep order, then tile the
            # whole stretch: time the model left out becomes "other".
            found.sort(key=lambda x: x["start"])
            snapped, previous = [], start
            for item in found:
                a = max(previous, snap(item["start"], onsets, start, end))
                b = min(end, snap(item["end"], offsets, start, end))
                if b > a:
                    snapped.append({**item, "start": a, "end": b})
                    previous = b
            for a, b, item in cover(start, end, snapped, MIN_DRAFT_S):
                unsure = item is None
                item = item or {"subtask_id": "", "subtask": "", "flags": ["模型未覆盖此时段"]}
                first, last = (int(i) for i in np.searchsorted(grid, [a, b]))
                last = max(first, min(last, len(grid)) - 1)
                hands = segment_hands(moving, first, max(first, last))
                task_text = "" if unsure else task.get("task", "")
                subtask_text = "" if unsure else item.get("subtask", "")
                flags = task_flags + item.get("flags", []) + pose_check(
                    item.get("subtask_id", ""), state, good, first, last, hands, gravity)
                if not task_text.strip() or not subtask_text.strip():
                    flags.append(UNSURE_FLAG)
                drafts.append({
                    "start_s": round(a, 3),
                    "end_s": math.floor(min(b, duration) * 1000) / 1000,
                    "task": task_text.strip() or OTHER,
                    "task_id": "" if not task_text.strip() else task.get("task_id", ""),
                    "subtask": subtask_text.strip() or OTHER,
                    "subtask_id": "" if not subtask_text.strip() else item.get("subtask_id", ""),
                    "hands": hands,
                    "description": "",
                    "flags": list(dict.fromkeys(flags)),
                })
    draft = {
        "created_unix_ns": time.time_ns(),
        "elapsed_s": round(time.monotonic() - started, 1),
        "model": model,
        "service_url": url,
        "action_table_version": table["version"] if table else "",
        "task": {k: task.get(k, "") for k in ("task_id", "task", "description")},
        "segments": drafts,
    }
    write_json(path / DRAFT, draft)
    return draft
