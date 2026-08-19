"""Loopback-only audio control and persistent voice settings."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable


DEFAULT_PLAYBACK_VOLUME = 40


def validate_playback_volume(value: object) -> int:
    """Return an integer percentage in [0, 100]."""
    if isinstance(value, bool):
        raise ValueError("playback volume must be an integer from 0 to 100")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "playback volume must be an integer from 0 to 100"
        ) from exc
    if not math.isfinite(numeric) or not numeric.is_integer() or not 0 <= numeric <= 100:
        raise ValueError("playback volume must be an integer from 0 to 100")
    return int(numeric)


def load_playback_volume(
    settings_path: Path,
    *,
    default: int = DEFAULT_PLAYBACK_VOLUME,
) -> int:
    """Load the persisted UI volume, falling back safely on missing/corrupt data."""
    fallback = validate_playback_volume(default)
    try:
        payload = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return fallback
        return validate_playback_volume(payload.get("playback_volume", fallback))
    except (OSError, json.JSONDecodeError, ValueError):
        return fallback


def _save_settings(settings_path: Path, updates: dict[str, object]) -> None:
    path = Path(settings_path)
    payload: dict[str, object] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = existing
        except (OSError, json.JSONDecodeError):
            pass
    payload.update(updates)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def save_playback_volume(settings_path: Path, value: object) -> int:
    """Atomically persist the UI-controlled playback volume."""
    volume = validate_playback_volume(value)
    _save_settings(settings_path, {"playback_volume": volume})
    return volume


class VoiceControlServer:
    """Expose audio status and volume updates to the local Dashboard proxy."""

    def __init__(
        self,
        host: str,
        port: int,
        status: Callable[[], dict[str, object]],
        set_volume: Callable[[object], dict[str, object]],
    ) -> None:
        self.host = host
        self.port = port
        self._status = status
        self._set_volume = set_volume
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        status = self._status
        set_volume = self._set_volume

        class Handler(BaseHTTPRequestHandler):
            def _json_response(self, status_code: int, payload: object) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path != "/v1/audio":
                    self._json_response(404, {"error": "not found"})
                    return
                self._json_response(200, status())

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path != "/v1/audio/volume":
                    self._json_response(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 4096:
                        raise ValueError("request body must contain a volume")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or "volume_percent" not in payload:
                        raise ValueError("field 'volume_percent' is required")
                    result = set_volume(payload["volume_percent"])
                except (json.JSONDecodeError, ValueError) as exc:
                    self._json_response(400, {"error": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001 - isolate control failures
                    self._json_response(409, {"error": str(exc)})
                    return
                self._json_response(200, result)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="voice_control_http",
        ).start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
