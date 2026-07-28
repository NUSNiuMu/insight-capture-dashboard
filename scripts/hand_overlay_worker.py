#!/usr/bin/env python3

"""Out-of-process hand-overlay JPEG compositing over authenticated AF_UNIX IPC."""

import argparse
import ctypes
import os
import signal
import sys
import threading
from multiprocessing.connection import Listener
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from hand_tracking.overlay import draw_hands_on_frame
from dashboard_media.jpeg import HwJpegCodec

_AUTHKEY_ENV = "INSIGHT_HANDOVERLAY_AUTHKEY"
_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
    """Use Linux PDEATHSIG to prevent an orphaned worker."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception:
        pass


def _log(message: str) -> None:
    print(f"[hand_overlay_worker] {message}", flush=True)


def _composite(
    hw_jpeg: Optional[HwJpegCodec], camera_name: str, jpeg_bytes: bytes, hands: List[Dict[str, object]]
) -> Optional[bytes]:
    """Decode, draw, and re-encode with hardware and cv2 fallback paths."""
    if hw_jpeg is not None:
        image = hw_jpeg.decode_jpeg_bgrx(camera_name, jpeg_bytes)
        if image is not None:
            draw_hands_on_frame(image, hands)
            encoded = hw_jpeg.encode_bgrx(camera_name, image, quality=90)
            if encoded is not None:
                return encoded
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if image is None:
        return None
    draw_hands_on_frame(image, hands)
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        return None
    return encoded.tobytes()


class _CameraWorker:
    """Composite only the newest pending frame for one camera."""

    def __init__(self, camera_name: str, hw_jpeg: Optional[HwJpegCodec], send: Callable[[Tuple], None]) -> None:
        self._camera_name = camera_name
        self._hw_jpeg = hw_jpeg
        self._send = send
        self._pending: Optional[Tuple[int, bytes, List[Dict[str, object]]]] = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        threading.Thread(target=self._run, daemon=True, name=f"hand_overlay:{camera_name}").start()

    def submit(self, version: int, jpeg_bytes: bytes, hands: List[Dict[str, object]]) -> None:
        with self._lock:
            self._pending = (version, jpeg_bytes, hands)
        self._event.set()

    def _run(self) -> None:
        while True:
            self._event.wait()
            with self._lock:
                item = self._pending
                self._pending = None
                self._event.clear()
            if item is None:
                continue
            version, jpeg_bytes, hands = item
            try:
                composited = _composite(self._hw_jpeg, self._camera_name, jpeg_bytes, hands)
            except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the worker
                _log(f"{self._camera_name}: composite failed ({exc})")
                continue
            if composited is not None:
                self._send((self._camera_name, version, composited))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipc-socket", required=True)
    return parser.parse_args()


def main() -> None:
    _die_with_parent()
    args = parse_args()

    authkey_hex = os.environ.get(_AUTHKEY_ENV)
    if not authkey_hex:
        print(f"hand_overlay_worker: missing {_AUTHKEY_ENV} env var; refusing to start", file=sys.stderr)
        sys.exit(1)
    # Decode the main process's hex-encoded IPC auth key.
    authkey = bytes.fromhex(authkey_hex)

    hw_jpeg = HwJpegCodec.create(log=_log)

    send_lock = threading.Lock()
    conn_holder: Dict[str, object] = {"conn": None}

    def send(message: Tuple) -> None:
        with send_lock:
            conn = conn_holder["conn"]
            if conn is None:
                return
            try:
                conn.send(message)
            except OSError:
                pass  # accept loop will notice on its next recv() and clean up

    workers: Dict[str, _CameraWorker] = {}
    workers_lock = threading.Lock()

    def dispatch(camera_name: str, version: int, jpeg_bytes: bytes, hands: List[Dict[str, object]]) -> None:
        with workers_lock:
            worker = workers.get(camera_name)
            if worker is None:
                worker = _CameraWorker(camera_name, hw_jpeg, send)
                workers[camera_name] = worker
        worker.submit(version, jpeg_bytes, hands)

    try:
        if os.path.exists(args.ipc_socket):
            os.unlink(args.ipc_socket)
    except OSError:
        pass
    listener = Listener(address=args.ipc_socket, family="AF_UNIX", authkey=authkey)
    _log(f"listening on {args.ipc_socket}")
    while True:
        conn = listener.accept()
        _log("main process connected")
        with send_lock:
            conn_holder["conn"] = conn
        try:
            while True:
                message = conn.recv()
                if not (isinstance(message, tuple) and len(message) == 4):
                    continue
                camera_name, version, jpeg_bytes, hands = message
                dispatch(camera_name, version, jpeg_bytes, hands)
        except (EOFError, OSError) as exc:
            _log(f"main process disconnected ({exc}); waiting for reconnect")
        finally:
            with send_lock:
                conn_holder["conn"] = None
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
