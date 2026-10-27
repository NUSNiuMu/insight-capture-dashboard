"""Bounded multi-view input scheduling and calibration identities."""

from dataclasses import dataclass
import hashlib
import json
import queue
import threading
import time

import numpy as np

from .synchronization import StereoPair


def calibration_identity(infos, transforms, *, rectification=None):
    """Identify geometry independently of message timestamps or quaternion sign."""
    payload = []
    for info in infos:
        payload.append([info.header.frame_id, int(info.width), int(info.height),
                        info.distortion_model, *np.round(info.k, 10), *np.round(info.d, 10)])
    for transform in transforms:
        matrix = np.asarray(transform, dtype=float).reshape(4, 4)
        if not np.isfinite(matrix).all():
            raise ValueError("Rig transforms must be finite")
        payload.append(np.round(matrix, 10).tolist())
    if rectification is not None:
        payload.append(sorted(rectification.items()))
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ViewPair(StereoPair):
    view: str = "front"


class LatestViewQueue:
    """Keep one pair per view and serve ready views in round-robin order."""

    def __init__(self, names):
        self.names = tuple(names)
        if not self.names or len(set(self.names)) != len(self.names):
            raise ValueError("Rig view names must be unique and nonempty")
        self.pending = {}
        self.cursor = 0
        self.stopped = False
        self.condition = threading.Condition()

    def put_nowait(self, value):
        with self.condition:
            if value is None:
                self.stopped = True
            else:
                if value.view not in self.names:
                    raise ValueError("Unknown rig view")
                self.pending[value.view] = value
            self.condition.notify()

    def get(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.condition:
            while not self.pending and not self.stopped:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self.condition.wait(remaining)
            if self.stopped:
                return None
            for offset in range(len(self.names)):
                index = (self.cursor + offset) % len(self.names)
                name = self.names[index]
                if name in self.pending:
                    self.cursor = (index + 1) % len(self.names)
                    return self.pending.pop(name)
            raise queue.Empty

    def get_nowait(self):
        return self.get(timeout=0)

    def clear(self):
        with self.condition:
            self.pending.clear()


def stereo_views(config):
    """Validate optional overlapping pairs without changing legacy profiles."""
    views = config.get("mapping", {}).get("stereo_views", [])
    if not views:
        return []
    if views[0].get("name") != "front":
        raise ValueError("The primary rig view must be named front")
    streams = {s["name"]: s for c in config["cameras"] for s in c.get("dashboard_streams", [])}
    names = set()
    required = {"name", "left_source", "right_source", "left_image_topic", "right_image_topic",
                "left_info_topic", "right_info_topic", "left_frame", "right_frame"}
    for view in views:
        if required - view.keys():
            raise ValueError(f"Incomplete stereo view: {sorted(required - view.keys())}")
        name = view["name"]
        if not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or name in names:
            raise ValueError("Invalid or duplicate stereo view name")
        names.add(name)
        if view.get("reference_eye", "left") not in ("left", "right"):
            raise ValueError("Invalid reference eye")
        if view["left_source"] == view["right_source"]:
            raise ValueError("Stereo view needs two different sources")
        for side in ("left", "right"):
            if view[side + "_source"] not in streams:
                raise ValueError("Stereo view references an unknown source")
            if streams[view[side + "_source"]].get("type") != "h264":
                raise ValueError("Ego rig sources must be H.264")
    topics = [view[side + suffix] for view in views for side in ("left", "right")
              for suffix in ("_image_topic", "_info_topic")]
    frames = [view[side + "_frame"] for view in views for side in ("left", "right")]
    if len(set(topics)) != len(topics) or len(set(frames)) != len(frames):
        raise ValueError("Rig output topics and frames must be unique")
    return views


class ReferenceSelector:
    """Explore views, then retain a geometric match long enough for consensus."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cursor = {}
        self.preferred = {}
        self.misses = {}

    def select(self, camera, references):
        with self.lock:
            preferred = references.get(self.preferred.get(camera))
            if preferred is not None:
                return preferred
            names = sorted(references)
            return references[names[self.cursor.get(camera, 0) % len(names)]] if names else None

    def advance(self, camera):
        with self.lock:
            self.cursor[camera] = self.cursor.get(camera, 0) + 1

    def observe(self, camera, view, accepted):
        with self.lock:
            if accepted:
                self.preferred[camera] = view
                self.misses[camera] = 0
            elif self.preferred.get(camera) == view:
                self.misses[camera] = self.misses.get(camera, 0) + 1
                if self.misses[camera] >= 2:
                    self.preferred.pop(camera, None)

    def reset(self):
        with self.lock:
            self.cursor.clear()
            self.preferred.clear()
            self.misses.clear()
