"""SuperGlue 本地推理使用的长度前缀 IPC 协议。

每条消息由“大端 32 位 JSON 头长度 + JSON 元数据 + 若干裸二进制数组”组成。显式
大小上限既防止损坏元数据导致无界分配，也让 healthcheck 与生产请求复用同一协议。
"""

from __future__ import annotations

import json
import socket
import struct
import sys
from typing import Dict, Iterable, List, Tuple


_LENGTH = struct.Struct("!I")
_MAX_HEADER_BYTES = 1_000_000
_MAX_PAYLOAD_BYTES = 32_000_000


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("inference socket closed before the frame completed")
        chunks.extend(chunk)
    return bytes(chunks)


def send_message(
    connection: socket.socket,
    metadata: Dict[str, object],
    payloads: Iterable[bytes] = (),
) -> None:
    """发送 JSON 元数据，以及由 ``payload_sizes`` 描述的连续二进制载荷。"""

    binary = tuple(bytes(payload) for payload in payloads)
    metadata = dict(metadata)
    metadata["payload_sizes"] = [len(payload) for payload in binary]
    header = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    if len(header) > _MAX_HEADER_BYTES:
        raise ValueError("inference IPC header is too large")
    if sum(len(payload) for payload in binary) > _MAX_PAYLOAD_BYTES:
        raise ValueError("inference IPC payload is too large")
    connection.sendall(_LENGTH.pack(len(header)))
    connection.sendall(header)
    for payload in binary:
        connection.sendall(payload)


def receive_message(
    connection: socket.socket,
) -> Tuple[Dict[str, object], List[bytes]]:
    """完整接收并验证一条元数据与二进制载荷消息。"""

    header_size = _LENGTH.unpack(_recv_exact(connection, _LENGTH.size))[0]
    if header_size <= 0 or header_size > _MAX_HEADER_BYTES:
        raise ValueError(f"invalid inference IPC header size: {header_size}")
    metadata = json.loads(_recv_exact(connection, header_size))
    sizes = metadata.pop("payload_sizes", [])
    if not isinstance(sizes, list) or any(
        not isinstance(size, int) or size < 0 for size in sizes
    ):
        raise ValueError("invalid inference IPC payload sizes")
    if sum(sizes) > _MAX_PAYLOAD_BYTES:
        raise ValueError("inference IPC payload exceeds the safety limit")
    return metadata, [_recv_exact(connection, size) for size in sizes]


def main() -> int:
    """通过真实协议探测 worker socket，供容器 healthcheck 使用。"""

    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} SOCKET", file=sys.stderr)
        return 2
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(2.0)
    try:
        connection.connect(sys.argv[1])
        send_message(connection, {"command": "health"})
        metadata, payloads = receive_message(connection)
    except Exception as exc:
        print(f"SuperGlue IPC health check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()
    if not metadata.get("ok") or payloads:
        print(f"SuperGlue IPC health check returned invalid data: {metadata}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
