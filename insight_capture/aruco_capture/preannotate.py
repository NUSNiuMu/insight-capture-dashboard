"""Background pre-annotation of a stopped recording.

1. 静止分段（双臂同时静止 >= 2 s）切出 task 级的动作段，复位现场的停顿丢弃。
2. VLM 服务（insight_capture.vlm_service）看全部动作段，认出整条录制的
   task（导入了动作表时从表中选），并写一句改写的英文说法。
3. 每个动作段按 40 s 窗口（重叠 5 s，不超过 45 s 的整段送）送头部相机
   画面，由模型切出带起止时间的 subtask。帧率按每次请求的视觉 token 上限
   定（最高 12 帧/秒），尽量多给画面；也可改为连同腕部鱼眼相机多路一起送
   （WRIST_VIEWS，实测在同样上限下没有更准）。
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

from insight_capture.vlm_service.prompt import CJK

from .capture import write_json
from .dataset import align, arm_stationary, connect, suggest_pause_segments

DRAFT = "segments_draft.json"
# One arm counts as idle when it moves for less than this share of the other
# arm's moving time (the free hand still drifts a little while holding still).
HANDS_RATIO = 0.3
FRAME_MAX_SIDE = 448
# Wrist cameras are fisheyes: the picture is the centre circle, so the frame is
# cropped to its centre square and sent smaller than the head view.
WRIST_MAX_SIDE = 256
TASK_FPS = 1.0
TASK_MAX_FRAMES = 200
# Frames per second sent for subtask timing: as many as the token budget
# allows in a window, up to SEGMENT_FPS.
SEGMENT_FPS = 12.0
# Visual tokens per /v1/segments request, all views together (32k peaked at
# 45 of 49 GB on the RTX 4090 48 GB running Qwen3.8-27B-FP8).
SEGMENT_TOKEN_BUDGET = 32000
MAX_WINDOW_S = 45.0
WINDOW_S = 40.0
# Also send the wrist fisheyes as synchronised videos. Off: measured on the
# cup-stacking recordings, it gave no better boundaries for the same budget.
WRIST_VIEWS = False
TOKEN_PATCH = 32          # vision patch 16 x spatial merge 2 (Qwen3-VL family)
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


def view_label(name):
    """头部相机 / 左夹爪腕部相机（上）… for a stored image stream name."""
    if name == "head":
        return "头部相机"
    parts = name.split("_")   # rgb_left_up
    side = {"left": "左", "right": "右"}.get(parts[1] if len(parts) > 1 else "", "")
    where = {"up": "上", "down": "下"}.get(parts[2] if len(parts) > 2 else "", "_".join(parts[2:]))
    return f"{side}夹爪腕部相机" + (f"（{where}）" if where else "") if side else name


def view_tokens(size):
    """Visual tokens one frame of a (width, height) view costs (two-frame patches)."""
    w, h = (max(1, round(x / TOKEN_PATCH)) for x in size)
    return w * h / 2


class CameraFrames:
    """Nearest JPEGs of one image stream from the session database, downscaled
    (wrist fisheyes cropped to their centre square first)."""

    def __init__(self, db, name="head"):
        self.db, self.name = db, name
        self.wrist = name != "head"
        self.max_side = WRIST_MAX_SIDE if self.wrist else FRAME_MAX_SIDE
        self.stamps = np.array(
            [r[0] for r in db.execute("SELECT t FROM samples WHERE kind='image' AND name=? ORDER BY t", (name,))]
        )
        if not len(self.stamps):
            raise ValueError("录制中没有观察相机画面，无法预标注" if name == "head" else f"录制中没有 {name} 画面")
        self.size = None
        sample = self.image(float(self.stamps[len(self.stamps) // 2]))
        self.size = sample.shape[1::-1] if sample is not None else (self.max_side, self.max_side)

    def image(self, stamp):
        payload = self.db.execute(
            "SELECT payload FROM samples WHERE kind='image' AND name=? AND t=?", (self.name, stamp)
        ).fetchone()[0]
        buffer = np.frombuffer(payload, np.uint8)
        # Halved while decoding: every view ends up well under 720 px anyway.
        image = cv2.imdecode(buffer, cv2.IMREAD_REDUCED_COLOR_2 if self.wrist else cv2.IMREAD_COLOR)
        if image is None:
            return None
        h, w = image.shape[:2]
        if self.wrist and w > h:
            x = (w - h) // 2
            image = image[:, x : x + h]
            w = h
        scale = min(1.0, self.max_side / max(h, w))
        if scale < 1:
            image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
        return image

    def encoded(self, times):
        """One JPEG per time, in order. A frame that does not decode is
        replaced by its neighbour rather than dropped: dropping would shift
        every later frame against the time axis the service assumes."""
        frames, missing = [], 0
        for t in times:
            i = int(np.clip(np.searchsorted(self.stamps, t), 0, len(self.stamps) - 1))
            if i and abs(self.stamps[i - 1] - t) < abs(self.stamps[i] - t):
                i -= 1
            image = self.image(float(self.stamps[i]))
            ok, buffer = (False, None) if image is None else cv2.imencode(
                ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                if frames:
                    frames.append(frames[-1])
                else:
                    missing += 1
                continue
            encoded = base64.b64encode(buffer.tobytes()).decode()
            frames += [encoded] * (missing + 1)   # leading failures take the first good frame
            missing = 0
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
MERGED_FLAG = "含模型未识别的时段，已并入本段"


def fold_unsure(segments):
    """Within one stretch of motion: a stretch of time the model could not label
    joins the subtask before it, and one at the start (nothing before it to
    join) is dropped, so a draft holds no "other" segments."""
    out = []
    for segment in segments:
        if segment["subtask"] != OTHER:
            out.append(segment)
        elif out:
            out[-1]["end_s"] = segment["end_s"]
            # The joined time was not understood: the result is less certain.
            if out[-1].get("confidence") is not None:
                out[-1]["confidence"] = round(out[-1]["confidence"] * 0.8, 2)
            if MERGED_FLAG not in out[-1]["flags"]:
                out[-1]["flags"].append(MERGED_FLAG)
    return out


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


def pause_draft(path, segmentation=None):
    """Draft of pause-detection boundaries only (no labels), written right after a
    recording stops; a model pre-annotation, when a service answers, replaces it."""
    started = time.monotonic()
    meta, grid, state, video, good, report, _reasons = align(path)
    segments = suggest_pause_segments(
        grid, state, good, report["fps"], duration_s=float(meta["duration_s"]), **(segmentation or {})
    )
    draft = {
        "created_unix_ns": time.time_ns(),
        "elapsed_s": round(time.monotonic() - started, 1),
        "source": "pause",
        "model": "",
        "service_url": "",
        "action_table_version": "",
        "segments": [{**segment, "description": "", "flags": []} for segment in segments],
    }
    write_json(path / DRAFT, draft)
    return draft


def has_model_draft(path):
    """A draft written by the labelling model (not just pause boundaries)."""
    file = path / DRAFT
    if not file.is_file():
        return False
    try:
        return json.loads(file.read_text()).get("source") != "pause"
    except ValueError:
        return False


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
    task = {"task_id": "", "task": "", "description": "", "flags": []}
    drafts = []
    with closing(connect(path)) as db:
        frames = CameraFrames(db)
        wrists = [
            CameraFrames(db, name)
            for (name,) in db.execute(
                "SELECT DISTINCT name FROM samples WHERE kind='image' AND name LIKE 'rgb%' ORDER BY name"
            )
        ] if WRIST_VIEWS else []
        views = [frames] + wrists
        per_instant = sum(view_tokens(v.size) for v in views)   # tokens per sampled instant
        plan = [(stretch, windows(*stretch)) for stretch in stretches]
        total, done = 1 + sum(len(w) for _, w in plan), 0
        progress(done, total)
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
                rate = min(SEGMENT_FPS, SEGMENT_TOKEN_BUDGET / (per_instant * (w1 - w0)))
                times = sample_times(w0, w1, rate)
                clips = [(view_label(v.name), v.encoded(times)) for v in views]
                # A view that lost frames would drift against the others: leave it out.
                clips = [c for c in clips if len(c[1]) == len(times)] or clips[:1]
                encoded = clips[0][1]
                answer = request_json(url, "/v1/segments", {
                    "frames": encoded, "fps": len(times) / (w1 - w0), "action_table": table,
                    "task_id": task.get("task_id", ""), "task": task.get("task", ""),
                    "views": [{"label": label, "frames": f} for label, f in clips] if len(clips) > 1 else None,
                }) if len(encoded) >= 2 else {"flags": ["可用画面不足，未送模型"]}
                # Keep what lies in this window's half of each overlap.
                lo = w0 + (WINDOW_OVERLAP_S / 2 if w0 > start else 0)
                hi = w1 - (WINDOW_OVERLAP_S / 2 if w1 < end else 0)
                # The service counts seconds from the first frame sent (its
                # timestamps are 2k/fps-based: frame 0 is time 0), and the first
                # frame sits half a frame interval after the window start.
                for item in answer.get("segments", []):
                    a, b = times[0] + item["start"], times[0] + item["end"]
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
            stretch_drafts = []
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
                stretch_drafts.append({
                    # Both ends rounded the same way, so a boundary shared by two
                    # segments stays one value (not 1 ms apart).
                    "start_s": round(a, 3),
                    "end_s": min(round(b, 3), math.floor(duration * 1000) / 1000),
                    "task": task_text.strip() or OTHER,
                    "task_id": "" if not task_text.strip() else task.get("task_id", ""),
                    "subtask": subtask_text.strip() or OTHER,
                    "subtask_id": "" if not subtask_text.strip() else item.get("subtask_id", ""),
                    "hands": hands,
                    "confidence": None if unsure else item.get("confidence"),
                    "description": "",
                    "flags": list(dict.fromkeys(flags)),
                })
            drafts += fold_unsure(stretch_drafts)
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


MODEL_LABEL_FLAG = "模型逐段标注，待确认"


GROUP_GAP_S = 10.0


def label_groups(spans):
    """Indices of ``spans`` (sorted by start) in groups sent as one clip: up to
    MAX_WINDOW_S from the first start to the last end, with no gap over
    GROUP_GAP_S; a longer segment is a group of its own."""
    order = sorted(range(len(spans)), key=lambda i: spans[i][0])
    groups = []
    for i in order:
        a, b = spans[i]
        if groups:
            first, last = spans[groups[-1][0]], spans[groups[-1][-1]]
            if b - first[0] <= MAX_WINDOW_S and a - last[1] <= GROUP_GAP_S:
                groups[-1].append(i)
                continue
        groups.append([i])
    return groups


def label_segments(path, url, table, segments, task_id="", progress=lambda done, total: None, segmentation=None,
                   context=()):
    """Label segments a person cut, one model request per segment; boundaries
    stay exactly as given. The task: ``task_id`` (what the person already chose
    for most segments), else the one the model draft found for the whole
    recording, else the model names it from every stretch of motion in the
    recording — a few short segments alone are too little to tell tasks apart.
    Neighbouring segments go to the model together (label_groups) so it can
    follow the sequence; ``context`` (every segment on the page, with its
    subtask text) supplies the label just before each group.
    Returns one label per segment, in order."""
    table = table if table and table.get("tasks") else None
    tasks = {t["id"]: t for t in (table or {}).get("tasks", [])}
    spans = [(float(s["start_s"]), float(s["end_s"])) for s in segments]
    if not spans or any(b <= a for a, b in spans):
        raise ValueError("片段起止时间无效")
    groups = label_groups(spans)
    total, done = len(groups) + 1, 0
    progress(done, total)
    if task_id not in tasks and has_model_draft(path):
        task_id = (json.loads((path / DRAFT).read_text()).get("task") or {}).get("task_id", "")
    whole = spans
    if task_id not in tasks:
        meta, grid, state, _video, good, report, _reasons = align(path)
        whole = [(s["start_s"], s["end_s"]) for s in suggest_pause_segments(
            grid, state, good, report["fps"], duration_s=float(meta["duration_s"]), **(segmentation or {})
        )] or spans
    with closing(connect(path)) as db:
        frames = CameraFrames(db)
        per_frame = view_tokens(frames.size)
        if task_id in tasks:
            task = {"task_id": task_id, "task": tasks[task_id]["text"], "flags": []}
        else:
            active_s = sum(b - a for a, b in whole)
            rate = min(TASK_FPS, TASK_MAX_FRAMES / active_s)
            times = np.concatenate([sample_times(a, b, rate) for a, b in whole])
            task = {"task_id": "", "task": "", "flags": [], **request_json(url, "/v1/task", {
                "frames": frames.encoded(times), "fps": rate, "action_table": table,
            })}
        done += 1
        progress(done, total)
        labels = [None] * len(spans)
        known = [(float(c["start_s"]), float(c["end_s"]), (c.get("subtask") or "").strip()) for c in context]
        for group in groups:
            g0, g1 = spans[group[0]][0], spans[group[-1]][1]
            # The label just before this group: a fresh one from this run wins.
            before = [(e, text) for s0, e, text in known if e <= g0 + 2e-3 and text]
            before += [(spans[i][1], labels[i]["subtask"]) for i in range(len(spans))
                       if labels[i] and labels[i]["subtask"] and spans[i][1] <= g0 + 2e-3]
            previous = max(before)[1] if before else ""
            rate = min(SEGMENT_FPS, SEGMENT_TOKEN_BUDGET / (per_frame * (g1 - g0)))
            times = sample_times(g0, g1, rate)
            encoded = frames.encoded(times)
            base = times[0]   # the service counts seconds from the first frame
            answer = request_json(url, "/v1/label", {
                "frames": encoded, "fps": len(times) / (g1 - g0), "action_table": table,
                "spans": [[round(spans[i][0] - base, 3), round(spans[i][1] - base, 3)] for i in group],
                "task_id": task.get("task_id", ""), "task": task.get("task", ""), "previous": previous,
            }) if len(encoded) >= 2 else {"flags": ["可用画面不足，未送模型"]}
            results = answer.get("labels") or [{"flags": answer.get("flags", [])}] * len(group)
            for i, result in zip(group, results):
                a, b = spans[i]
                flags = [MODEL_LABEL_FLAG] + list(task.get("flags", [])) + list(result.get("flags", []))
                subtask = (result.get("subtask") or "").strip()
                labels[i] = {
                    "start_s": a, "end_s": b,
                    "task": (task.get("task") or "").strip(),
                    "task_id": task.get("task_id", "") if (task.get("task") or "").strip() else "",
                    "subtask": subtask,
                    "subtask_id": result.get("subtask_id", "") if subtask else "",
                    "confidence": result.get("confidence"),
                    "flags": list(dict.fromkeys(flags)),
                }
            done += 1
            progress(done, total)
    return labels


def translate(url, items, table=None):
    """Chinese task / subtask text -> English, matched to the action table when
    the meaning is the same. Items without Chinese are returned unchanged."""
    if not items:
        return []
    asks = [i for i, item in enumerate(items) if any(CJK.search(item.get(k) or "") for k in ("task", "subtask"))]
    out = [{"task": item.get("task", ""), "subtask": item.get("subtask", ""), "task_id": "", "subtask_id": "",
            "flags": []} for item in items]
    if not asks:
        return out
    answer = request_json(url, "/v1/translate", {
        "items": [{"task": items[i].get("task", ""), "subtask": items[i].get("subtask", "")} for i in asks],
        "action_table": table if table and table.get("tasks") else None,
    }, timeout=300)
    if not answer.get("items"):
        raise ValueError("模型没能完成转换：" + ("；".join(answer.get("flags", [])) or "未知原因"))
    for i, row in zip(asks, answer["items"]):
        out[i] = {k: row.get(k, "") for k in ("task", "subtask", "task_id", "subtask_id")} | {"flags": row.get("flags", [])}
    return out
