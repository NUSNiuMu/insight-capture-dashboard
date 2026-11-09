"""Background pre-annotation of a stopped recording.

Boundaries come from the same pause detection as the「静止分段」button, the
participating hands from which arm actually moves in the pose data, and
task / subtask text and action-table ids from the VLM labelling service
(insight_capture.vlm_service). The result is a draft next to the session
(segments_draft.json); segments.json is only written when a person saves.
"""

import base64
from contextlib import closing
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import cv2
import numpy as np

from .capture import write_json
from .dataset import align, arm_stationary, connect, suggest_pause_segments

DRAFT = "segments_draft.json"
MIN_SEGMENT_S = 1.5
# One arm counts as idle when it moves for less than this share of the other
# arm's moving time (the free hand still drifts a little while holding still).
HANDS_RATIO = 0.3
SAMPLE_FPS = 2.0
MAX_FRAMES = 24
FRAME_MAX_SIDE = 672


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


def sample_times(start, end, count):
    count = max(2, count - count % 2)  # Qwen video patches span two frames
    step = (end - start) / count
    return start + step / 2 + step * np.arange(count)


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


def request_label(url, body, timeout=180):
    request = urllib.request.Request(
        url.rstrip("/") + "/v1/label",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:  # the service answered: flag this segment only
        try:
            reason = json.load(exc).get("error", "")
        except ValueError:
            reason = ""
        return {"flags": [f"推理服务报错 {exc.code}：{reason}".rstrip("：")]}
    except (OSError, ValueError) as exc:
        raise ServiceDown(f"推理服务连接中断：{exc}") from exc


def run(path, url, table=None, model="", progress=lambda done, total: None):
    """Pre-annotate one stopped session and write its draft; returns the draft."""
    started = time.monotonic()
    meta, grid, state, video, good, report, _reasons = align(path)
    fps = report["fps"]
    segments = suggest_pause_segments(grid, state, good, fps, duration_s=meta["duration_s"])
    pair_good = good[:-1] & good[1:]
    moving = {
        side: pair_good & ~arm_stationary(state, fps, offset)
        for side, offset in (("left", 0), ("right", 10))
    }
    table = table if table and table.get("tasks") else None
    drafts = []
    with closing(connect(path)) as db:
        frames = HeadFrames(db)
        for i, segment in enumerate(segments):
            progress(i, len(segments))
            start, end = segment["start_s"], segment["end_s"]
            first, last = np.searchsorted(grid, [start, end])
            hands = segment_hands(moving, int(first), max(int(first), int(last) - 1))
            count = int(min(MAX_FRAMES, max(4, (end - start) * SAMPLE_FPS)))
            times = sample_times(start, end, count)
            encoded = frames.encoded(times)
            if len(encoded) < 2:
                label = {"flags": ["片段内可用画面不足，未送模型"]}
            else:
                label = request_label(
                    url,
                    {
                        "frames": encoded,
                        "fps": len(times) / (end - start),
                        "hands": hands,
                        "action_table": table,
                    },
                )
            flags = list(label.get("flags", []))
            if end - start < MIN_SEGMENT_S:
                flags.append(f"片段较短（{end - start:.1f} s），请确认是否与相邻片段合并")
            drafts.append(
                {
                    **segment,
                    "task": label.get("task", ""),
                    "subtask": label.get("subtask", ""),
                    "task_id": label.get("task_id", ""),
                    "subtask_id": label.get("subtask_id", ""),
                    "hands": hands,
                    "description": label.get("description", ""),
                    "flags": flags,
                }
            )
    progress(len(segments), len(segments))
    draft = {
        "created_unix_ns": time.time_ns(),
        "elapsed_s": round(time.monotonic() - started, 1),
        "model": model,
        "service_url": url,
        "action_table_version": table["version"] if table else "",
        "segments": drafts,
    }
    write_json(path / DRAFT, draft)
    return draft
