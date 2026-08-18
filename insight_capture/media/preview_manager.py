"""Viewer leases and lazy media-worker lifecycle."""

from __future__ import annotations

import threading
import time


class PreviewManager:
    def __init__(self, owner, *, lease_sec: float = 5.0, idle_stop_sec: float = 45.0) -> None:
        self.owner = owner
        self.lease_sec = max(1.0, float(lease_sec))
        self.idle_stop_sec = max(self.lease_sec, float(idle_stop_sec))
        self._last_activity = 0.0
        self._last_demand = 0.0
        self._websocket_viewers = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._monitor, daemon=True, name="preview_lifecycle").start()

    def activity(self, *, ensure_webrtc: bool = True) -> None:
        with self._lock:
            now = time.monotonic()
            self._last_activity = now
            self._last_demand = now
        if ensure_webrtc:
            self.owner._worker_supervisor.ensure_webrtc_worker()

    def viewer_connected(self) -> None:
        with self._lock:
            self._websocket_viewers += 1
        self.activity()

    def viewer_disconnected(self) -> None:
        with self._lock:
            self._websocket_viewers = max(0, self._websocket_viewers - 1)
            self._last_activity = time.monotonic()

    def requested(self) -> bool:
        with self._lock:
            leased = time.monotonic() - self._last_activity <= self.lease_sec
            viewers = self._websocket_viewers > 0
        return leased or viewers or any(self.owner._webrtc_has_sessions.values())

    def status(self) -> dict:
        proc = getattr(self.owner, "_webrtc_proc", None)
        return {
            "mode": "review" if self.requested() else "capture",
            "preview_active": self.requested(),
            "webrtc_worker_running": proc is not None and proc.poll() is None,
            "websocket_viewers": self._websocket_viewers,
        }

    def _monitor(self) -> None:
        while not self._stop.wait(1.0):
            if any(self.owner._webrtc_has_sessions.values()):
                with self._lock:
                    self._last_demand = time.monotonic()
                continue
            proc = getattr(self.owner, "_webrtc_proc", None)
            if proc is None:
                continue
            with self._lock:
                idle = time.monotonic() - max(self._last_activity, self._last_demand)
                viewers = self._websocket_viewers
            if viewers == 0 and idle >= self.idle_stop_sec:
                self.owner.stop_webrtc_worker()
                self.owner.stop_hand_overlay_worker()

    def close(self) -> None:
        self._stop.set()
