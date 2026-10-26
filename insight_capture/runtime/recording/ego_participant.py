"""EgoHalo's idempotent recorder and verified, read-only TF-card export client."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class EgoParticipant:
    def __init__(self, config: dict):
        self.base = str(config["ego_url"]).rstrip("/")
        self.timeout = float(config.get("http_timeout_sec", 4))
        self.http = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path: str, body=None, *, headers=None, binary=False):
        request = Request(self.base + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", "X-Capture-Device-Profile": "egohalo", **(headers or {})})
        with self.http.open(request, timeout=self.timeout) as response:
            data = response.read()
            return (data, response.headers) if binary else json.loads(data)

    def preflight(self) -> dict:
        capability = self.request("/api/capture/participant")
        if capability.get("protocol") != 1 or capability.get("coordinator"):
            raise RuntimeError("Ego 未提供独立录制接口")
        status = self.request("/api/status")
        if status["recorder"]["state"] != "idle":
            raise RuntimeError("Ego 正在录制或保存，请先结束原录制")
        blockers = [item["message"] for item in status["readiness"]["blockers"] if item["code"] != "task"]
        storage = status["storage"]
        if not storage.get("mounted") or not storage.get("writable") or not storage.get("media", {}).get("ready"):
            blockers.append("Ego TF 卡未挂载或不可写")
        if storage.get("free_bytes", 0) < max(storage.get("reserve_bytes", 0), 5 * 1024**3):
            blockers.append("Ego TF 卡剩余空间不足 5 GB")
        if blockers:
            raise RuntimeError("Ego 未就绪：" + "；".join(blockers))
        return status

    def task(self, name: str, token: str) -> str:
        headers = {"X-Capture-Media": token}
        tasks = self.request("/api/tasks")["tasks"]
        task = next((item for item in tasks if item["name"].casefold() == name.casefold()), None)
        if task is None:
            task = self.request("/api/tasks", {"name": name}, headers=headers)["task"]
        else:
            self.request("/api/tasks/" + quote(task["id"], safe="") + "/activate", {}, headers=headers)
        return task["id"]

    def lookup(self, session: dict):
        return self.request("/api/recording/commands/" + session["start_command"]).get("recording")

    def cancel(self, session: dict):
        # A persisted cancellation also prevents a delayed start from creating a new take.
        self.request("/api/capture/cancel", {"session_id": session["session_id"]})
        record = self.lookup(session)
        if record and record["status"] in {"recording", "preparing"}:
            self.request("/api/recording/stop", {"command_id": session["stop_command"], "recording_id": record["id"]})
        return record

    def download(self, session: dict, destination: Path) -> dict:
        """Copy immutable ranges, verify every remote chunk, and retain the TF original."""
        headers = {"X-Looper-Client": "upload", "Host": urlsplit(self.base).hostname}
        token = self.request("/api/export/v1/pair", {}, headers=headers)["token"]
        headers["Authorization"] = "Bearer " + token
        entries = self.request("/api/export/v1/recordings", headers=headers)["sessions"]
        entry = next((item for item in entries if item["id"] == session["remote_recording_id"]), None)
        if entry is None:
            raise RuntimeError("Ego 尚未提供该条已保存录制")
        destination.mkdir(parents=True, exist_ok=True)
        total = sum(item["bytes"] for item in entry["files"])
        if shutil.disk_usage(destination).free < total + 1024**3:
            raise RuntimeError("Jetson 空间不足，Ego 原始数据仍保留在 TF 卡")
        files = []
        self.request("/api/export/v1/lease", {"action": "acquire"}, headers=headers)
        try:
            for item in entry["files"]:
                name, size = item["name"], item["bytes"]
                if Path(name).name != name or name in {".", ".."} or not isinstance(size, int) or size <= 0:
                    raise ValueError("Invalid Ego export file descriptor")
                target = destination / name
                partial = target.with_name(name + ".partial")
                digest = hashlib.sha256()
                with partial.open("wb") as handle:
                    for start in range(0, size, 4 * 1024**2):
                        self.request("/api/export/v1/lease", {"action": "renew"}, headers=headers)
                        end = min(size, start + 4 * 1024**2) - 1
                        data, response = self.request(
                            "/api/export/v1/recordings/" + quote(entry["id"], safe="") + "/files/" + quote(name, safe=""),
                            headers={**headers, "Range": f"bytes={start}-{end}", "If-Match": '"' + entry["revision"] + '"'}, binary=True)
                        if (len(data) != end - start + 1 or hashlib.sha256(data).hexdigest() != response.get("X-Chunk-SHA256")
                                or response.get("Content-Range") != f"bytes {start}-{end}/{size}"):
                            raise RuntimeError("Ego 文件分片校验失败")
                        handle.write(data)
                        digest.update(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(partial, target)
                files.append({"name": name, "bytes": size, "sha256": digest.hexdigest()})
        finally:
            self.request("/api/export/v1/lease", {"action": "release"}, headers=headers)
        capture = json.loads((destination / "capture.json").read_text())
        if capture.get("session_id") != session["session_id"] or capture.get("save_state") != "complete":
            raise RuntimeError("Ego 导出 session_id 或保存状态不匹配")
        return {"device_id": entry["deviceId"], "revision": entry["revision"], "files": files, "verified_at_epoch_s": time.time()}
