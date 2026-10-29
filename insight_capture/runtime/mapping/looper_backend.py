"""Framed raw-image transport to the single-owner LooperVIO C++ backend."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import struct
import threading

import numpy as np


class LooperBackend:
    def __init__(self, socket_path: Path):
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(30.0)
        self._socket.connect(str(socket_path))
        self._lock = threading.Lock()

    def _read(self, length: int) -> bytes:
        result = bytearray(length)
        view = memoryview(result)
        while view:
            received = self._socket.recv_into(view)
            if received == 0:
                raise ConnectionError("LooperVIO backend disconnected")
            view = view[received:]
        return bytes(result)

    def request(self, command: str, *, images=(), arrays=(), **metadata):
        frames = [np.ascontiguousarray(array) for array in (*images, *arrays)]
        if any(a.ndim != 2 or a.dtype not in (np.uint8, np.float32) for a in frames):
            raise ValueError("LooperVIO expects 2D uint8 or float32 arrays")
        payload = json.dumps({"command": command, **metadata, "images": [
            {"width": image.shape[1], "height": image.shape[0], "dtype": str(image.dtype)} for image in frames
        ]}, separators=(",", ":")).encode()
        with self._lock:
            self._socket.sendall(struct.pack("!I", len(payload)) + payload)
            for image in frames:
                self._socket.sendall(memoryview(image))
            size = struct.unpack("!I", self._read(4))[0]
            result = json.loads(self._read(size))
            arrays = []
            for spec in result.pop("arrays", []):
                rows, cols = int(spec["height"]), int(spec["width"])
                if rows < 0 or cols < 0 or rows * cols > 8_388_608:
                    raise ValueError("Invalid native mapping response dimensions")
                dtype = np.dtype(spec["dtype"])
                arrays.append(np.frombuffer(self._read(rows * cols * dtype.itemsize), dtype=dtype).reshape(rows, cols))
        if not result["ok"]:
            raise RuntimeError(result["error"])
        if arrays:
            result["arrays"] = arrays
        return result

    def close(self):
        self._socket.close()
