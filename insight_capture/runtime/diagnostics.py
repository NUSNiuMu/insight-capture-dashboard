"""Shared input evidence and bounded diagnostic events."""

from collections import deque
from datetime import datetime
import json
import threading
import time

from insight_capture.media.rate import observed_frame_rate


def input_snapshot(owner, now=None):
    now = time.monotonic() if now is None else now
    timeout = owner.camera_stale_timeout_sec
    with owner.camera_input_lock:
        samples = {name: list(values) for name, values in owner.camera_input_times.items()}
    result = {}
    for camera in owner.cameras:
        values = samples.get(camera.name, [])
        image_age = now - values[-1] if values else None
        vio_seen = owner.camera_liveness_times.get(camera.name, 0.)
        vio_age = now - vio_seen if vio_seen else None
        result[camera.name] = {
            "image_state": "receiving" if image_age is not None and image_age <= timeout else "no_input",
            "vio_state": "receiving" if vio_age is not None and vio_age <= timeout else "no_input",
            "input_age_sec": image_age,
            "input_fps": observed_frame_rate(values, now),
            "input_samples_window": len(values),
            "native_vio_age_sec": vio_age,
        }
    return result


class DiagnosticEvents:
    def __init__(self, logger):
        self.logger = logger
        self.lock = threading.Lock()
        self.states = {}
        self.events = deque(maxlen=100)

    def changed(self, component, device, state, detail="", level="info", **evidence):
        key = (component, device)
        identity = (state, detail)
        with self.lock:
            previous = self.states.get(key)
            if previous is not None and previous[0] == identity:
                return
            now = time.monotonic()
            event = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                     "component": component, "device": device, "state": state,
                     "detail": detail, **evidence}
            if previous is not None:
                event["previous_state_duration_sec"] = round(now - previous[1], 2)
            self.states[key] = (identity, now)
            self.events.append(event)
        message = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        # rclpy binds a call site's severity on its first invocation.
        if level == "warning":
            self.logger.warning(message)
        elif level == "error":
            self.logger.error(message)
        else:
            self.logger.info(message)

    def snapshot(self):
        with self.lock:
            return list(self.events)
