"""Evidence-based system diagnostics and explicit repairs for capture appliances."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


STATUS_RANK = {"PASS": 0, "INFO": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}
STATUS_LABEL = {
    "PASS": "正常",
    "INFO": "信息",
    "SKIP": "跳过",
    "WARN": "警告",
    "FAIL": "故障",
}


@dataclasses.dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    unavailable: bool = False


@dataclasses.dataclass
class Finding:
    check_id: str
    section: str
    status: str
    summary: str
    evidence: list[str] = dataclasses.field(default_factory=list)
    impact: str | None = None
    fixes: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class RepairOutcome:
    finding: Finding
    attempted: bool
    target_check_ids: tuple[str, ...] = ()


class Runner:
    """Run bounded, non-interactive commands."""

    def run(self, argv: Sequence[str], *, timeout: float = 8.0) -> CommandResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
            return CommandResult(
                argv=list(argv),
                returncode=completed.returncode,
                stdout=completed.stdout.strip(),
                stderr=completed.stderr.strip(),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except FileNotFoundError as exc:
            return CommandResult(
                argv=list(argv),
                returncode=127,
                stdout="",
                stderr=str(exc),
                duration_ms=round((time.monotonic() - started) * 1000),
                unavailable=True,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return CommandResult(
                argv=list(argv),
                returncode=124,
                stdout=stdout.strip(),
                stderr=(stderr.strip() or f"command timed out after {timeout:g}s"),
                duration_ms=round((time.monotonic() - started) * 1000),
            )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    text = _read_text(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _key_values(text: str, separator: str = "=") -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if separator not in raw_line:
            continue
        key, value = raw_line.split(separator, 1)
        values[key.strip()] = value.strip()
    return values


def _human_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024.0 or unit == "TiB":
            return f"{number:.1f} {unit}"
        number /= 1024.0
    return f"{number:.1f} TiB"


def _short_error(result: CommandResult) -> str:
    detail = result.stderr or result.stdout or f"exit {result.returncode}"
    return detail.splitlines()[-1][:240]


def parse_chrony_tracking(text: str) -> dict[str, Any]:
    """Extract stable chrony fields without depending on locale spacing."""

    raw = _key_values(text, ":")
    parsed: dict[str, Any] = dict(raw)
    offset_match = re.search(
        r"System time\s*:\s*([+-]?[0-9.eE]+) seconds (fast|slow)", text
    )
    if offset_match:
        value = float(offset_match.group(1))
        parsed["system_offset_seconds"] = value if offset_match.group(2) == "fast" else -value
    last_match = re.search(r"Last offset\s*:\s*([+-]?[0-9.eE]+) seconds", text)
    if last_match:
        parsed["last_offset_seconds"] = float(last_match.group(1))
    stratum_match = re.search(r"Stratum\s*:\s*(\d+)", text)
    if stratum_match:
        parsed["stratum"] = int(stratum_match.group(1))
    return parsed


def parse_camera_ntp_offsets(text: str) -> dict[str, float]:
    """Parse the read-only ntpdate report emitted by sync_camera_restart."""

    offsets: dict[str, float] = {}
    pattern = re.compile(
        r"^\s*(insight(?:3_[ab]|9_a))\s+([+-]?[0-9]+(?:\.[0-9]+)?)\s+ms\s*$"
    )
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            offsets[match.group(1)] = float(match.group(2))
    return offsets


def parse_camera_phase_result(text: str) -> dict[str, float | str] | None:
    """Extract the final image timestamp phase verdict from the sync tool."""

    match = re.search(
        r"^Result:\s+(PASS|FAIL)\s+\(max observed skew\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+ms,\s+limit\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+ms\)$",
        text,
        re.MULTILINE,
    )
    if not match:
        return None
    return {
        "verdict": match.group(1),
        "max_skew_ms": float(match.group(2)),
        "limit_ms": float(match.group(3)),
    }


def _camera_repair_evidence(text: str) -> list[str]:
    """Keep the useful before/after timing lines without embedding raw JSON."""

    evidence: list[str] = []
    section = ""
    headings = {
        "Current NTP offsets",
        "Post-sync NTP offsets",
        "Final NTP offsets",
        "Restart timer results",
        "LPWM configuration events",
        "Image timestamp measurement",
    }
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line in headings:
            section = line
            evidence.append(section)
            continue
        if not line:
            continue
        if re.match(r"^insight(?:3_[ab]|9_a)\s+", line):
            evidence.append(f"{section}: {line}" if section else line)
        elif line.startswith("estimated spread"):
            evidence.append(f"{section}: {line}" if section else line)
        elif line.startswith("Result:"):
            evidence.append(line)
    return evidence


def _parse_compose_rows(text: str) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows


def _meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    text = _read_text(Path("/proc/meminfo")) or ""
    for line in text.splitlines():
        match = re.match(r"([^:]+):\s+(\d+)\s+kB", line)
        if match:
            result[match.group(1)] = int(match.group(2)) * 1024
    return result


def _network_snapshot() -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    text = _read_text(Path("/proc/net/dev")) or ""
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, fields = line.split(":", 1)
        values = fields.split()
        if len(values) >= 12:
            result[name.strip()] = (int(values[3]), int(values[11]))
    return result


def _softnet_drops() -> int | None:
    text = _read_text(Path("/proc/net/softnet_stat"))
    if text is None:
        return None
    total = 0
    try:
        for line in text.splitlines():
            fields = line.split()
            if len(fields) > 1:
                total += int(fields[1], 16)
        return total
    except ValueError:
        return None


def _tail_lines(path: Path, maximum_bytes: int = 512 * 1024) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - maximum_bytes))
            data = handle.read().decode("utf-8", errors="replace")
        return data.splitlines()[-500:]
    except OSError:
        return []


def _existing_staging_entries(roots: Iterable[Path]) -> list[Path]:
    entries: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            normalized = root.resolve()
            if normalized in seen or not normalized.is_dir():
                continue
            seen.add(normalized)
            entries.extend(sorted(normalized.iterdir()))
        except OSError:
            continue
    return entries


def _error_lines(lines: Iterable[str], limit: int = 12) -> list[str]:
    severe = re.compile(
        r"traceback|segmentation fault|out of memory|cuda.*(?:error|failed)|"
        r"fatal|uncaught|unhandled|\bexception\b|\berror\b|\bfailed\b",
        re.IGNORECASE,
    )
    benign = re.compile(
        r"0 (?:errors|failed)|no (?:error|failure)|expected.*failure|"
        r"failure_count[=:]\s*0",
        re.IGNORECASE,
    )
    matches = []
    for line in lines:
        compact = re.sub(r"\s+", " ", line).strip()
        if compact and severe.search(compact) and not benign.search(compact):
            matches.append(compact[-300:])
    return matches[-limit:]


class SystemDoctor:
    def __init__(
        self,
        *,
        root: Path,
        api_url: str,
        log_since: str,
        sample_seconds: float,
        runner: Runner | None = None,
    ) -> None:
        self.root = root
        self.api_url = api_url.rstrip("/")
        self.log_since = log_since
        self.sample_seconds = sample_seconds
        self.runner = runner or Runner()
        self.findings: list[Finding] = []
        self.context: dict[str, Any] = {}

    def add(
        self,
        check_id: str,
        section: str,
        status: str,
        summary: str,
        *,
        evidence: Iterable[str] = (),
        impact: str | None = None,
        fixes: Iterable[str] = (),
    ) -> None:
        self.findings.append(
            Finding(
                check_id=check_id,
                section=section,
                status=status,
                summary=summary,
                evidence=[str(item) for item in evidence if str(item)],
                impact=impact,
                fixes=[str(item) for item in fixes if str(item)],
            )
        )

    def fetch_json(self, path: str, *, timeout: float = 8.0) -> tuple[Any | None, str | None]:
        url = f"{self.api_url}{path}"
        return self.fetch_url_json(url, timeout=timeout)

    @staticmethod
    def fetch_url_json(
        url: str, *, timeout: float = 8.0
    ) -> tuple[Any | None, str | None]:
        try:
            request = Request(
                url,
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urlopen(request, timeout=timeout) as response:
                return json.load(response), None
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except OSError:
                detail = ""
            return None, f"HTTP {exc.code}: {detail or exc.reason}"
        except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError, OSError) as exc:
            return None, str(exc)

    def check_configuration(self) -> None:
        profile = _read_text(self.root / "config/.device")
        camera_path = self.root / "config/cameras.json"
        runtime_path = self.root / "config/runtime.json"
        try:
            cameras = _load_json(camera_path)
            enabled = [item for item in cameras.get("cameras", []) if item.get("enabled", True)]
            ros_domain = cameras.get("ros_domain_id")
            self.context["cameras"] = enabled
            self.context["ros_domain_id"] = ros_domain
            evidence = [
                f"profile={profile or 'unknown'}",
                f"ROS_DOMAIN_ID={ros_domain}",
                "enabled_cameras=" + ", ".join(str(item.get("name")) for item in enabled),
            ]
            status = "PASS"
            summary = f"配置可解析，启用 {len(enabled)} 台相机"
            fixes: list[str] = []
            if len(enabled) != 3:
                status = "FAIL"
                summary = f"相机配置数量异常：启用 {len(enabled)} 台，现场流程要求 3 台"
                fixes = ["检查 config/cameras.json，并用 scripts/select_device.sh <profile> 恢复正确模板。"]
            self.add("config.live", "配置", status, summary, evidence=evidence, fixes=fixes)
        except (OSError, ValueError, TypeError) as exc:
            self.add(
                "config.live",
                "配置",
                "FAIL",
                "live 配置无法读取或 JSON 无效",
                evidence=[f"{camera_path}: {exc}"],
                impact="Dashboard 可能无法启动，或订阅错误的相机/ROS domain。",
                fixes=[f"验证 JSON：python3 -m json.tool {camera_path}"],
            )
            return

        if profile:
            template_dir = self.root / "config/devices" / profile
            mismatches = []
            for live in (camera_path, runtime_path):
                template = template_dir / live.name
                if not template.exists() or not live.exists():
                    continue
                try:
                    if _load_json(live) != _load_json(template):
                        mismatches.append(live.name)
                except (OSError, ValueError):
                    pass
            if mismatches:
                self.add(
                    "config.profile_drift",
                    "配置",
                    "WARN",
                    "live 配置与所选设备模板不一致",
                    evidence=[f"profile={profile}", "different=" + ", ".join(mismatches)],
                    impact="差异可能是有意的现场参数，也可能是切 profile 后残留。",
                    fixes=[
                        "先保存并审查 live 配置差异；确认未在录制后，再决定是否运行 "
                        f"scripts/select_device.sh {profile}。"
                    ],
                )
            else:
                self.add("config.profile_drift", "配置", "PASS", "live 配置与设备模板一致")

    def check_time(self) -> None:
        date_result = self.runner.run(["date", "--iso-8601=seconds"])
        timedate = self.runner.run(
            [
                "timedatectl",
                "show",
                "-p",
                "NTPSynchronized",
                "-p",
                "NTP",
                "-p",
                "TimeUSec",
                "-p",
                "RTCTimeUSec",
                "-p",
                "Timezone",
            ]
        )
        chrony = self.runner.run(["chronyc", "tracking"])
        sources = self.runner.run(["chronyc", "sources", "-n"])
        evidence = [f"host_time={date_result.stdout or _short_error(date_result)}"]
        status = "PASS"
        summary = "宿主机 NTP 已同步"
        fixes: list[str] = []

        timedate_values: dict[str, str] = {}
        if timedate.returncode == 0:
            timedate_values = _key_values(timedate.stdout)
            evidence.extend(
                f"{key}={timedate_values.get(key, 'unknown')}"
                for key in ("NTPSynchronized", "NTP", "Timezone")
            )
            if timedate_values.get("NTPSynchronized", "").lower() != "yes":
                status = "FAIL"
                summary = "宿主机 NTP 未同步"
                fixes.extend(
                    [
                        "检查 chrony：systemctl status chrony && chronyc sources -v",
                        "确认网络/DNS 可达 NTP 源后运行：sudo systemctl restart chrony",
                    ]
                )
            if timedate_values.get("Timezone") not in {"Asia/Shanghai", None}:
                status = "WARN" if status == "PASS" else status
                evidence.append("expected_timezone=Asia/Shanghai")
                fixes.append("修正时区：sudo timedatectl set-timezone Asia/Shanghai")
        else:
            status = "WARN"
            summary = "无法通过 systemd 验证宿主机 NTP 状态"
            evidence.append(f"timedatectl={_short_error(timedate)}")
            fixes.append("在宿主机而不是受限容器内运行本脚本，或执行 timedatectl status。")

        if chrony.returncode == 0:
            tracking = parse_chrony_tracking(chrony.stdout)
            offset = tracking.get("system_offset_seconds")
            leap = tracking.get("Leap status", "unknown")
            reference = tracking.get("Reference ID", "unknown")
            evidence.extend(
                [
                    f"chrony_reference={reference}",
                    f"chrony_stratum={tracking.get('stratum', 'unknown')}",
                    f"chrony_system_offset={float(offset) * 1000:+.3f} ms" if offset is not None else "chrony_system_offset=unknown",
                    f"chrony_leap_status={leap}",
                ]
            )
            selected = next(
                (line.strip() for line in sources.stdout.splitlines() if line.lstrip().startswith("^*")),
                None,
            )
            if selected:
                evidence.append(f"selected_source={selected}")
            if str(leap).lower() != "normal":
                status = "FAIL"
                summary = f"chrony 未进入正常同步状态：{leap}"
                fixes.append("检查 chronyc sources -v 中是否有 ^* 选中源及 Reach 是否正常。")
            elif offset is not None and abs(float(offset)) > 0.1:
                status = "FAIL"
                summary = f"宿主机时钟偏差过大：{float(offset) * 1000:+.1f} ms"
            elif offset is not None and abs(float(offset)) > 0.01 and status == "PASS":
                status = "WARN"
                summary = f"宿主机已同步，但当前时钟偏差偏大：{float(offset) * 1000:+.1f} ms"
                fixes.append("等待 chrony 收敛并复查 chronyc tracking；持续不收敛时检查 NTP 网络质量。")
        else:
            evidence.append(f"chronyc={_short_error(chrony)}")
            if timedate_values.get("NTPSynchronized", "").lower() == "no":
                status = "FAIL"
            elif status == "PASS":
                status = "WARN"
                summary = "系统报告 NTP 已同步，但无法取得具体源和偏差"
            fixes.append("安装/启用 chrony，或确认 chronyd socket 对当前用户可读。")

        self.add(
            "time.host_ntp",
            "时间同步",
            status,
            summary,
            evidence=evidence,
            impact="未同步或大幅偏差会破坏多相机时间对齐、日志排序和录制目录时间。" if status != "PASS" else None,
            fixes=fixes,
        )

    def check_resources(self) -> None:
        cpu_count = os.cpu_count() or 1
        try:
            load_1, load_5, load_15 = os.getloadavg()
            ratio = load_1 / cpu_count
            status = "FAIL" if ratio >= 1.5 else "WARN" if ratio >= 0.9 else "PASS"
            self.add(
                "resource.cpu_load",
                "主机资源",
                status,
                f"1 分钟负载 {load_1:.2f}，占 {cpu_count} 核容量的 {ratio:.0%}",
                evidence=[f"load_1m={load_1:.2f}", f"load_5m={load_5:.2f}", f"load_15m={load_15:.2f}"],
                impact="持续高负载会造成图像回调、定位和录制排队。" if status != "PASS" else None,
                fixes=["停止与采集并行的优化/导出任务，并用 top -H 或 docker stats 定位高占用进程。"] if status != "PASS" else (),
            )
        except OSError as exc:
            self.add("resource.cpu_load", "主机资源", "WARN", "无法读取系统负载", evidence=[str(exc)])

        mem = _meminfo()
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", 0)
        ratio = available / total if total else 0.0
        swap_total = mem.get("SwapTotal", 0)
        swap_used = max(0, swap_total - mem.get("SwapFree", 0))
        status = "FAIL" if total and ratio < 0.08 else "WARN" if total and ratio < 0.15 else "PASS"
        if swap_total and swap_used / swap_total > 0.5 and status == "PASS":
            status = "WARN"
        self.add(
            "resource.memory",
            "主机资源",
            status,
            f"可用内存 {_human_bytes(available)} / {_human_bytes(total)}，Swap 已用 {_human_bytes(swap_used)}",
            evidence=[f"available_ratio={ratio:.1%}" if total else "available_ratio=unknown"],
            impact="内存压力和换页会造成不可预测的录制掉帧。" if status != "PASS" else None,
            fixes=["用 ps aux --sort=-rss 和 docker stats 找出内存占用；不要在录制中强制重启服务。"] if status != "PASS" else (),
        )

        seen_devices: set[int] = set()
        for label, path in (("系统盘", Path("/")), ("录制目录", self.root / "rosbags")):
            try:
                stat = os.stat(path)
                if stat.st_dev in seen_devices:
                    continue
                seen_devices.add(stat.st_dev)
                usage = shutil.disk_usage(path)
                free_ratio = usage.free / usage.total if usage.total else 0.0
                status = "FAIL" if usage.free < 5 * 1024**3 or free_ratio < 0.03 else "WARN" if usage.free < 20 * 1024**3 or free_ratio < 0.10 else "PASS"
                self.add(
                    f"resource.disk.{stat.st_dev}",
                    "主机资源",
                    status,
                    f"{label} {path} 剩余 {_human_bytes(usage.free)}（{free_ratio:.1%}）",
                    evidence=[f"total={_human_bytes(usage.total)}", f"used={_human_bytes(usage.used)}"],
                    impact="磁盘耗尽会让录制中断或产生不完整 bag。" if status != "PASS" else None,
                    fixes=["在 /bags 确认数据已备份后清理旧 bag；不要删除 rosbags/_staging 中待恢复数据。"] if status != "PASS" else (),
                )
            except OSError as exc:
                self.add("resource.disk", "主机资源", "WARN", f"无法检查 {label}", evidence=[str(exc)])

        temperatures = []
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            value = _read_int(zone / "temp")
            if value is None:
                continue
            celsius = value / 1000.0 if value > 1000 else float(value)
            temperatures.append((_read_text(zone / "type") or zone.name, celsius))
        if temperatures:
            hottest_name, hottest = max(temperatures, key=lambda item: item[1])
            status = "FAIL" if hottest >= 90 else "WARN" if hottest >= 80 else "PASS"
            self.add(
                "resource.temperature",
                "主机资源",
                status,
                f"最高温度 {hottest:.1f} °C（{hottest_name}）",
                evidence=[f"{name}={value:.1f} °C" for name, value in sorted(temperatures, key=lambda item: item[1], reverse=True)[:8]],
                impact="热降频会降低相机处理和定位吞吐。" if status != "PASS" else None,
                fixes=["检查风扇、风道和 Jetson 功耗模式；降温后再复测。"] if status != "PASS" else (),
            )
        else:
            self.add("resource.temperature", "主机资源", "SKIP", "系统未暴露 thermal zone 温度")

    def check_network(self) -> None:
        ip_result = self.runner.run(["ip", "-j", "address", "show"])
        camera_interfaces: list[str] = []
        addresses: list[str] = []
        if ip_result.returncode == 0:
            try:
                rows = json.loads(ip_result.stdout)
                for row in rows:
                    local_addresses = [
                        str(info.get("local"))
                        for info in row.get("addr_info", [])
                        if info.get("family") == "inet" and str(info.get("local", "")).startswith("169.254.")
                    ]
                    if local_addresses:
                        camera_interfaces.append(str(row.get("ifname")))
                        addresses.extend(f"{row.get('ifname')}={address}" for address in local_addresses)
                expected = len(self.context.get("cameras", []))
                status = "PASS" if len(camera_interfaces) >= expected else "FAIL"
                self.add(
                    "network.camera_links",
                    "相机网络",
                    status,
                    f"检测到 {len(camera_interfaces)} 个相机链路网卡，配置要求 {expected} 个",
                    evidence=addresses or ["未发现 169.254.x.x IPv4 地址"],
                    impact="缺失链路对应的相机不会发布图像、IMU 或 VIO。" if status != "PASS" else None,
                    fixes=["检查缺失相机的 USB 线和供电；停止录制后可运行 scripts/reboot_cameras.sh。"] if status != "PASS" else (),
                )
            except (json.JSONDecodeError, TypeError) as exc:
                self.add("network.camera_links", "相机网络", "WARN", "无法解析网卡地址", evidence=[str(exc)])
        else:
            self.add(
                "network.camera_links",
                "相机网络",
                "WARN",
                "无法读取宿主机网卡，未能验证相机物理链路",
                evidence=[_short_error(ip_result)],
                fixes=["在宿主机运行：ip -4 -br addr | grep 169.254"],
            )

        expected_sysctls = {
            "net.core.rmem_max": 67_108_864,
            "net.core.rmem_default": 67_108_864,
            "net.core.netdev_max_backlog": 8192,
            "net.ipv4.ipfrag_high_thresh": 134_217_728,
            "net.ipv4.ipfrag_max_dist": 4096,
        }
        bad = []
        evidence = []
        unavailable = []
        for key, minimum in expected_sysctls.items():
            value = _read_int(Path("/proc/sys") / key.replace(".", "/"))
            if value is None:
                unavailable.append(key)
                continue
            evidence.append(f"{key}={value} (minimum {minimum})")
            if value < minimum:
                bad.append(key)
        if bad:
            self.add(
                "network.kernel_tuning",
                "相机网络",
                "FAIL",
                f"{len(bad)} 个 DDS/UDP 内核参数低于采集要求",
                evidence=evidence + (["unreadable=" + ", ".join(unavailable)] if unavailable else []),
                impact="大图 UDP 包可能在进入 DDS 订阅前被丢弃，导致 bag 间歇掉帧。",
                fixes=["运行 sudo ./deploy/host_setup.sh 持久化正确参数，然后重录短 bag 验证。"],
            )
        elif unavailable:
            self.add(
                "network.kernel_tuning",
                "相机网络",
                "WARN",
                "部分内核收包参数不可读，无法完成掉帧风险验证",
                evidence=evidence + ["unreadable=" + ", ".join(unavailable)],
                fixes=["在宿主机运行本脚本；若仍不可读，逐项执行 sysctl <name>。"],
            )
        else:
            self.add("network.kernel_tuning", "相机网络", "PASS", "DDS/UDP 内核收包参数满足要求", evidence=evidence)

        rps_bad = []
        rps_evidence = []
        rps_service = self.runner.run(
            [
                "systemctl",
                "show",
                "insight-camera-network.service",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "Result",
                "-p",
                "ExecMainStatus",
                "-p",
                "ExecStart",
            ]
        )
        rps_service_values = (
            _key_values(rps_service.stdout) if rps_service.returncode == 0 else {}
        )
        if rps_service_values.get("LoadState") != "not-found":
            rps_evidence.append(
                "insight-camera-network.service: "
                f"active={rps_service_values.get('ActiveState', 'unknown')}, "
                f"result={rps_service_values.get('Result', 'unknown')}, "
                f"exit={rps_service_values.get('ExecMainStatus', 'unknown')}"
            )
            if rps_service_values.get("ExecStart"):
                rps_evidence.append(
                    "service_exec=" + rps_service_values["ExecStart"][:300]
                )
        for interface in camera_interfaces:
            masks = []
            for path in Path(f"/sys/class/net/{interface}/queues").glob("rx-*/rps_cpus"):
                mask = _read_text(path)
                if mask is not None:
                    masks.append(mask)
                    rps_evidence.append(f"{interface}/{path.parent.name}={mask}")
            if masks and all(set(mask.replace(",", "").replace("0", "")) == set() for mask in masks):
                rps_bad.append(interface)
        if rps_bad:
            service_failed = (
                rps_service_values.get("LoadState") == "loaded"
                and rps_service_values.get("ActiveState") != "active"
            )
            self.add(
                "network.rps",
                "相机网络",
                "FAIL",
                (
                    "相机网卡 RPS 未启用，且自动配置服务异常："
                    if service_failed
                    else "相机网卡 RPS 未启用："
                )
                + ", ".join(rps_bad),
                evidence=rps_evidence,
                impact="网络协议处理会集中在单核，突发流量可能溢出 softnet backlog。",
                fixes=[
                    "运行 sudo ./deploy/host_setup.sh 重装并启动 RPS systemd/udev 配置。",
                    "复验：systemctl status insight-camera-network.service；所有相机 rx-0/rps_cpus 应非 00。",
                ],
            )
        elif rps_evidence:
            self.add("network.rps", "相机网络", "PASS", "相机网卡 RPS 已启用", evidence=rps_evidence)
        else:
            self.add("network.rps", "相机网络", "SKIP", "没有可检查的相机 RX 队列")

        before_links = _network_snapshot()
        before_softnet = _softnet_drops()
        time.sleep(self.sample_seconds)
        after_links = _network_snapshot()
        after_softnet = _softnet_drops()
        growing = []
        delta_evidence = []
        for interface in camera_interfaces:
            before = before_links.get(interface)
            after = after_links.get(interface)
            if before and after:
                rx_delta = after[0] - before[0]
                tx_delta = after[1] - before[1]
                delta_evidence.append(f"{interface}: rx_drop_delta={rx_delta}, tx_drop_delta={tx_delta}")
                if rx_delta > 0 or tx_delta > 0:
                    growing.append(interface)
        softnet_delta = None
        if before_softnet is not None and after_softnet is not None:
            softnet_delta = after_softnet - before_softnet
            delta_evidence.append(f"softnet_drop_delta={softnet_delta}")
        status = "FAIL" if growing or (softnet_delta or 0) > 0 else "PASS"
        self.add(
            "network.drop_sample",
            "相机网络",
            status,
            f"{self.sample_seconds:g} 秒采样内" + ("检测到新增网络丢包" if status == "FAIL" else "未检测到新增网卡/softnet 丢包"),
            evidence=delta_evidence or ["计数器不可读"],
            impact="增长中的内核或网卡丢包会直接造成录制缺帧。" if status == "FAIL" else None,
            fixes=["先运行 sudo ./deploy/host_setup.sh；录制时仍增长则保存 recording_network_audit.json 并检查 USB 链路。"] if status == "FAIL" else (),
        )

    def check_containers(self) -> None:
        compose = self.runner.run(
            ["docker", "compose", "ps", "-a", "--format", "json"], timeout=12
        )
        if compose.returncode != 0:
            self.add(
                "containers.compose",
                "容器",
                "FAIL",
                "无法读取 Docker Compose 服务状态",
                evidence=[_short_error(compose)],
                impact="无法确认 Dashboard、建图和定位服务是否实际运行。",
                fixes=["确认 Docker daemon 正常且当前用户有权限：docker info；再执行 docker compose ps -a。"],
            )
            return
        rows = _parse_compose_rows(compose.stdout)
        required = {
            "insight-dashboard",
            "superglue-inference",
            "insight9-sparse-mapper",
            "insight3-global-localizer",
        }
        by_service = {str(row.get("Service")): row for row in rows}
        failures = []
        evidence = []
        names = []
        for service in sorted(required):
            row = by_service.get(service)
            if row is None:
                failures.append(f"{service}=missing")
                continue
            name = str(row.get("Name") or row.get("Names") or "")
            if name:
                names.append(name)
            state = str(row.get("State") or "unknown")
            health = str(row.get("Health") or "none")
            evidence.append(f"{service}: state={state}, health={health}, name={name}")
            if state.lower() != "running" or health.lower() in {"unhealthy", "starting"}:
                failures.append(f"{service}={state}/{health}")
        status = "FAIL" if failures else "PASS"
        self.add(
            "containers.required",
            "容器",
            status,
            "核心容器全部运行" if not failures else f"{len(failures)} 个核心容器缺失或异常",
            evidence=evidence + failures,
            impact="对应的网页、建图或全局定位功能不可用。" if failures else None,
            fixes=["先看失败服务日志：docker compose logs --tail 200 <service>；确认原因后执行 docker compose up -d <service>。"] if failures else (),
        )

        invalid_time = []
        restarts = []
        inspect_evidence = []
        for name in names:
            result = self.runner.run(
                [
                    "docker",
                    "inspect",
                    name,
                    "--format",
                    "{{json .State}}|{{.RestartCount}}",
                ]
            )
            if result.returncode != 0 or "|" not in result.stdout:
                continue
            state_text, restart_text = result.stdout.rsplit("|", 1)
            try:
                state = json.loads(state_text)
                restart_count = int(restart_text)
            except (ValueError, json.JSONDecodeError):
                continue
            started = str(state.get("StartedAt") or "")
            inspect_evidence.append(f"{name}: started={started}, restarts={restart_count}, oom={state.get('OOMKilled')}")
            if started.startswith("1970-"):
                invalid_time.append(name)
            if restart_count >= 3 or state.get("OOMKilled"):
                restarts.append(name)
        if restarts:
            self.add(
                "containers.restarts",
                "容器",
                "WARN",
                "容器累计重启次数较高或存在 OOM 记录：" + ", ".join(restarts),
                evidence=inspect_evidence,
                fixes=["检查 docker inspect <container> 和近期日志；若 OOMKilled=true，先定位内存压力。"],
            )
        else:
            self.add("containers.restarts", "容器", "PASS", "未发现频繁重启或 OOM", evidence=inspect_evidence)
        if invalid_time:
            self.add(
                "containers.start_time",
                "容器",
                "WARN",
                "容器启动时间记录为 1970，运行时长显示不可信",
                evidence=["affected=" + ", ".join(invalid_time)],
                impact="这是主机启动后时钟被 NTP 大幅校正留下的 Docker 元数据，不等同于服务运行了几十年。",
                fixes=["确认当前 NTP 正常；仅在没有录制/任务时重建受影响服务以刷新时间元数据：docker compose up -d --force-recreate <service>。"],
            )
        else:
            self.add("containers.start_time", "容器", "PASS", "容器启动时间元数据正常")

    def _preflight_fixes(self, code: str) -> list[str]:
        return {
            "camera_count": ["检查 config/cameras.json 和当前 profile。"],
            "camera_stale": ["检查对应 169.254 网卡、相机供电和 USB；未录制时等待 watchdog 或重启 Dashboard。"],
            "mapping_not_ready": ["查看 /api/mapping；确认 Insight9 有图像/VIO 输入，并让相机在标定场景中产生运动。"],
            "localization_not_ready": ["让 Insight3 看到标定地图中的有效纹理；根据 rejection 字段判断是匹配数、内点还是覆盖不足。"],
            "pose_stale": ["先恢复相机 VIO/建图定位数据；不要只重启网页。"],
            "storage_unwritable": ["检查 findmnt、挂载读写状态和目录权限；修复后重启 Dashboard 重新探测。"],
            "storage_fallback": ["核对 .env 的 INSIGHT_ROSBAG_HOST_DIR 与 INSIGHT_ROSBAG_REQUIRED_SOURCE；修复挂载后重启 Dashboard。"],
            "storage_low": ["备份并从 /bags 清理旧录制，保留 _staging 待恢复数据。"],
            "topic_discovery_failed": ["确认 ROS_DOMAIN_ID 和 DDS 实现后刷新 topics；必要时查看 Dashboard 日志。"],
            "recorder_topics_missing": ["用 ros2 topic list -t 确认缺失发布者；恢复相机/定位服务后再次运行。"],
        }.get(code, ["查看该检查的 details 和 Dashboard 近期日志后处理。"])

    def check_dashboard(self) -> None:
        paths = {
            "health": "/healthz",
            "system": "/api/system/status",
            "cameras": "/api/cameras",
            "recording": "/api/recording/status",
            "images": "/api/images/capabilities",
            "mapping": "/api/mapping",
        }
        payloads: dict[str, Any] = {}
        errors: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as pool:
            futures = {
                name: pool.submit(self.fetch_json, path, timeout=12.0)
                for name, path in paths.items()
            }
            for name, future in futures.items():
                payload, error = future.result()
                if error:
                    errors[name] = error
                else:
                    payloads[name] = payload

        health = payloads.get("health")
        if not isinstance(health, dict) or not health.get("ok"):
            self.add(
                "dashboard.http",
                "Dashboard",
                "FAIL",
                "Dashboard healthz 不可用",
                evidence=[errors.get("health", f"response={health!r}")],
                impact="网页、录制控制和深度状态 API 不可用。",
                fixes=["执行 docker compose ps，再查看 docker logs insight-dashboard --since 30m。"],
            )
            return
        self.add(
            "dashboard.http",
            "Dashboard",
            "PASS",
            "Dashboard HTTP 服务正常",
            evidence=[f"url={self.api_url}/healthz", f"fake_pose={health.get('fake_pose')}"] + [f"optional_api_error[{name}]={error}" for name, error in errors.items() if name != "health"],
        )

        system = payloads.get("system") if isinstance(payloads.get("system"), dict) else {}
        preflight = system.get("preflight") if isinstance(system.get("preflight"), dict) else {}
        failures = preflight.get("failures") if isinstance(preflight.get("failures"), list) else []
        warnings = preflight.get("warnings") if isinstance(preflight.get("warnings"), list) else []
        if failures or warnings:
            evidence = []
            fixes = []
            for item in [*failures, *warnings]:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or "unknown")
                evidence.append(f"{code}: {item.get('message')} | details={json.dumps(item.get('details') or {}, ensure_ascii=False, sort_keys=True)}")
                fixes.extend(self._preflight_fixes(code))
            self.add(
                "dashboard.preflight",
                "采集就绪",
                "FAIL" if failures else "WARN",
                f"录制前检发现 {len(failures)} 个故障、{len(warnings)} 个警告",
                evidence=evidence,
                impact="当前不满足可靠采集条件。" if failures else "可采集但存在需要确认的降级项。",
                fixes=list(dict.fromkeys(fixes)),
            )
        elif preflight:
            storage = preflight.get("storage") or {}
            self.add(
                "dashboard.preflight",
                "采集就绪",
                "PASS",
                "录制前检通过",
                evidence=[f"free={_human_bytes(storage.get('free_bytes'))}", f"topics_missing={len((preflight.get('topics') or {}).get('missing') or [])}"],
            )
        else:
            self.add("dashboard.preflight", "采集就绪", "WARN", "未取得录制前检结果", evidence=[errors.get("system", "missing preflight payload")])

        cameras = payloads.get("cameras") if isinstance(payloads.get("cameras"), dict) else {}
        camera_rows = cameras.get("cameras") if isinstance(cameras.get("cameras"), list) else []
        if camera_rows:
            for camera in camera_rows:
                name = str(camera.get("name") or "unknown")
                age = camera.get("input_age_sec")
                source_fps = ((camera.get("webrtc_stats") or {}).get("input_fps")) or camera.get("fps") or 0.0
                stale = bool(camera.get("stale")) or age is None
                low_fps = not stale and float(source_fps) < 20.0
                status = "FAIL" if stale else "WARN" if low_fps else "PASS"
                summary = (
                    f"{name} 无新鲜图像"
                    if stale
                    else f"{name} 输入 {float(source_fps):.2f} fps，帧率偏低"
                    if low_fps
                    else f"{name} 图像输入正常：{float(source_fps):.2f} fps"
                )
                self.add(
                    f"camera.{name}",
                    "相机数据",
                    status,
                    summary,
                    evidence=[f"topic={camera.get('topic')}", f"input_age_sec={age}", f"stale={camera.get('stale')}"],
                    impact="该相机图像无法可靠预览或录制。" if status != "PASS" else None,
                    fixes=["先查物理链路和对应 topic；停止录制后再重启服务或相机。"] if status != "PASS" else (),
                )
        else:
            self.add("camera.api", "相机数据", "WARN", "相机状态 API 无数据", evidence=[errors.get("cameras", "empty cameras list")])

        recording = payloads.get("recording") if isinstance(payloads.get("recording"), dict) else {}
        self.context["recording_active"] = bool(recording.get("recording"))
        storage = recording.get("storage") if isinstance(recording.get("storage"), dict) else {}
        if storage.get("using_fallback"):
            storage_evidence = [
                f"configured={storage.get('configured_path')}",
                f"active={storage.get('active_path')}",
                f"required_source={storage.get('required_source')}",
                f"mounted_source={storage.get('mounted_source')}",
                f"reason={storage.get('fallback_reason')}",
            ]
            storage_fixes = self._preflight_fixes("storage_fallback")
            env_values = _key_values(_read_text(self.root / ".env") or "")
            host_dir = env_values.get("INSIGHT_ROSBAG_HOST_DIR")
            required_source = env_values.get("INSIGHT_ROSBAG_REQUIRED_SOURCE")
            if host_dir:
                mount = self.runner.run(
                    [
                        "findmnt",
                        "-J",
                        "-T",
                        host_dir,
                        "-o",
                        "TARGET,SOURCE,FSTYPE,OPTIONS",
                    ]
                )
                if mount.returncode == 0:
                    try:
                        filesystems = json.loads(mount.stdout).get("filesystems") or []
                        filesystem = filesystems[0] if filesystems else {}
                    except (json.JSONDecodeError, AttributeError, IndexError):
                        filesystem = {}
                    target = str(filesystem.get("target") or "")
                    source = str(filesystem.get("source") or "")
                    storage_evidence.extend(
                        [
                            f"host_path={host_dir}",
                            f"host_findmnt_target={target or 'unknown'}",
                            f"host_findmnt_source={source or 'unknown'}",
                        ]
                    )
                    if required_source and source and not source.startswith(required_source):
                        storage_fixes = [
                            f"{host_dir} 当前实际位于 {source}（挂载点 {target}），不是要求的 {required_source}；先挂载正确录制盘，不要仅取消安全校验。",
                            "确认 findmnt -T <录制目录> 的 SOURCE 正确且 rw 后，重启 Dashboard 让它重新选择主存储。",
                        ]
            self.add(
                "recording.storage",
                "录制存储",
                "WARN",
                "录制正在使用备用存储",
                evidence=storage_evidence,
                impact="数据没有写入预期录制盘；备用盘耗尽时会影响采集。",
                fixes=storage_fixes,
            )
        elif recording:
            self.add("recording.storage", "录制存储", "PASS", "录制存储使用预期路径", evidence=[f"active={storage.get('active_path')}"])
        recovery_lines = [str(line) for line in recording.get("recent_output", []) if "unrecoverable" in str(line).lower() or "nothing recoverable" in str(line).lower()]
        staging_roots = [self.root / "rosbags/_staging"]
        configured_host_dir = _key_values(
            _read_text(self.root / ".env") or ""
        ).get("INSIGHT_ROSBAG_HOST_DIR")
        if configured_host_dir:
            configured_path = Path(configured_host_dir)
            if not configured_path.is_absolute():
                configured_path = self.root / configured_path
            staging_roots.append(configured_path / "_staging")
        active_path = str(storage.get("active_path") or "")
        project_container_prefix = "/workspaces/insight_capture/"
        if active_path.startswith(project_container_prefix):
            staging_roots.append(
                self.root
                / active_path.removeprefix(project_container_prefix)
                / "_staging"
            )
        staging_entries = _existing_staging_entries(staging_roots)
        if staging_entries:
            self.add(
                "recording.recovery",
                "录制存储",
                "WARN",
                "发现未恢复的中断录制 staging 数据",
                evidence=[f"path={path}" for path in staging_entries]
                + recovery_lines[-6:],
                impact="这些旧录制尚不可作为完整数据使用，但应保留用于取证。",
                fixes=["不要直接删除 _staging；记录目录名，先备份并用 check_bag.py/SQLite 工具评估可恢复性。"],
            )
        else:
            self.add(
                "recording.recovery",
                "录制存储",
                "PASS",
                "磁盘上没有待处理的中断录制 staging 数据",
                evidence=(
                    ["Dashboard API 仍含启动时历史恢复日志，因对应目录已不存在而忽略"]
                    if recovery_lines
                    else []
                ),
            )

        runtime = cameras.get("runtime") if isinstance(cameras.get("runtime"), dict) else (system.get("runtime") or {})
        if runtime and not runtime.get("webrtc_worker_running"):
            self.add(
                "media.webrtc_worker",
                "图像管线",
                "FAIL",
                "WebRTC worker 未运行",
                impact="浏览器实时视频会不可用或退回 JPEG。",
                fixes=["查看 outputs/webrtc_worker.log 和主容器日志；确认原因后在未录制时重启 Dashboard。"],
            )
        elif runtime:
            self.add("media.webrtc_worker", "图像管线", "PASS", "WebRTC worker 正在运行")

        images = payloads.get("images") if isinstance(payloads.get("images"), dict) else {}
        active_path = images.get("active_path")
        disabled = ((images.get("hw_jpeg") or {}).get("disabled")) or []
        if active_path != "jpeg-hardware-nvjpeg" or disabled:
            self.add(
                "media.hardware",
                "图像管线",
                "WARN",
                f"图像编码未完全使用 NVJPEG 硬件路径：{active_path}",
                evidence=[f"hw_jpeg_disabled={disabled}", f"elements={json.dumps((images.get('gstreamer') or {}).get('elements') or {}, sort_keys=True)}"],
                impact="软件编码会显著提高 CPU 占用并可能挤压录制。",
                fixes=["docker logs insight-dashboard | grep hw_jpeg；确认 nvidia runtime 和 nvjpegenc 后，在空闲时重启。"],
            )
        elif images:
            self.add(
                "media.hardware",
                "图像管线",
                "PASS",
                "NVJPEG 与硬件 H.264 图像管线可用",
                evidence=[f"active_path={active_path}", f"hardware_encoder={images.get('hardware_encoder')}", f"webrtc_ready={images.get('webrtc_ready')}"],
            )

        mapping = payloads.get("mapping") if isinstance(payloads.get("mapping"), dict) else {}
        statuses = mapping.get("statuses") if isinstance(mapping.get("statuses"), dict) else {}
        if statuses:
            evidence = []
            bad = []
            for name, value in statuses.items():
                if not isinstance(value, dict):
                    continue
                evidence.append(
                    f"{name}: online={value.get('online')}, state={value.get('state')}, localized={value.get('localized')}, rejection={value.get('rejection')}, map_points={value.get('map_point_count')}"
                )
                if not value.get("online") or value.get("state") == "error":
                    bad.append(str(name))
            self.add(
                "mapping.services",
                "建图定位",
                "FAIL" if bad else "PASS",
                "建图/定位状态发布正常" if not bad else "建图/定位服务异常：" + ", ".join(bad),
                evidence=evidence,
                fixes=["检查对应 compose 服务日志和 SuperGlue health；根据 rejection 字段排查输入或场景匹配。"] if bad else (),
            )

    def check_logs(self) -> None:
        recent_evidence = []
        compose_logs = self.runner.run(
            [
                "docker",
                "compose",
                "logs",
                "--no-color",
                "--since",
                self.log_since,
                "--tail",
                "500",
            ],
            timeout=15,
        )
        if compose_logs.returncode == 0:
            recent_evidence.extend(_error_lines(compose_logs.stdout.splitlines()))
        file_evidence = []
        for name in ("backend_crash.log", "webrtc_worker.log", "hand_overlay_worker.log", "voice_control_worker.log"):
            matches = _error_lines(_tail_lines(self.root / "outputs" / name), limit=4)
            file_evidence.extend(f"{name}: {line}" for line in matches)
        if recent_evidence:
            self.add(
                "logs.recent_errors",
                "日志",
                "WARN",
                f"最近 {self.log_since} 的容器日志中发现 {len(recent_evidence)} 条错误特征",
                evidence=recent_evidence[-16:] + file_evidence[-8:],
                impact="日志关键字不一定代表当前故障，需结合时间和上方健康检查确认。",
                fixes=[f"展开复核：docker compose logs --since {self.log_since} --no-color；不要只按 error 关键字直接重启。"],
            )
        elif compose_logs.returncode == 0:
            self.add("logs.recent_errors", "日志", "PASS", f"最近 {self.log_since} 未发现高风险错误特征")
        else:
            self.add("logs.recent_errors", "日志", "WARN", "无法收集 Compose 日志", evidence=[_short_error(compose_logs)])
        if file_evidence:
            self.add(
                "logs.worker_tail",
                "日志",
                "INFO",
                "持久化 worker 日志尾部包含历史错误，不能据此判断仍在发生",
                evidence=file_evidence[-12:],
                fixes=["结合文件时间戳与上方实时健康状态复核；需要时运行 stat outputs/*.log。"],
            )

    def check_voice(self) -> None:
        service = self.runner.run(
            [
                "systemctl",
                "--user",
                "show",
                "insight-voice-control.service",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
            ]
        )
        if service.returncode == 0:
            values = _key_values(service.stdout)
            if values.get("LoadState") == "not-found":
                self.add("voice.service", "语音", "SKIP", "未安装可选语音控制服务")
            elif values.get("ActiveState") == "active" and values.get("SubState") == "running":
                self.add("voice.service", "语音", "PASS", "语音控制服务正在运行", evidence=[service.stdout.replace("\n", ", ")])
            else:
                self.add(
                    "voice.service",
                    "语音",
                    "WARN",
                    "语音控制服务未运行",
                    evidence=[service.stdout.replace("\n", ", ")],
                    impact="语音开始/停止、报错播报不可用；网页手动操作不受影响。",
                    fixes=["systemctl --user restart insight-voice-control.service；再看 journalctl --user -u insight-voice-control.service -n 100。"],
                )
        else:
            self.add(
                "voice.service",
                "语音",
                "SKIP",
                "当前执行环境无法访问用户级 systemd，未验证语音服务",
                evidence=[_short_error(service)],
            )

    def check_camera_clocks(self) -> None:
        identity = os.environ.get("INSIGHT_CAMERA_SSH_IDENTITY")
        password = os.environ.get("INSIGHT_CAMERA_SSH_PASSWORD")
        if not identity and not password:
            self.add(
                "time.camera_clocks",
                "时间同步",
                "INFO",
                "未取得相机 NTP offset：缺少相机 SSH 凭据",
                evidence=[
                    "measurement=需要在相机内执行只读 ntpdate -q <host-link-ip>",
                    "actions=未执行时间同步、相机重启或相位调整",
                ],
                fixes=[
                    "使用 ./scripts/system_doctor.sh 时输入相机 SSH 密码；密码只通过环境传给只读检查。"
                ],
            )
            return

        command = [
            sys.executable,
            str(self.root / "scripts/sync_camera_restart.py"),
            "--check-only",
        ]
        if identity:
            command.extend(["--identity-file", identity])
        result = self.runner.run(command, timeout=90.0)
        offsets_by_camera = parse_camera_ntp_offsets(result.stdout)
        expected_names = {"insight3_a", "insight3_b", "insight9_a"}
        if result.returncode != 0 or set(offsets_by_camera) != expected_names:
            self.add(
                "time.camera_clocks",
                "时间同步",
                "WARN",
                "相机 NTP offset 只读查询失败",
                evidence=[
                    line
                    for line in [result.stdout, result.stderr]
                    if line
                ][-4:]
                + ["actions=未执行时间同步、相机重启或相位调整"],
                fixes=["确认三台相机 SSH 可达且密码/identity 正确，然后重新运行诊断。"],
            )
            return

        offsets = list(offsets_by_camera.values())
        max_host_offset = max(abs(offset) for offset in offsets)
        camera_skew = max(offsets) - min(offsets)
        if max_host_offset > 50.0 or camera_skew > 50.0:
            status = "FAIL"
            summary = (
                f"相机 NTP 时差过大：最大主机时差 {max_host_offset:.3f} ms，"
                f"相机间差 {camera_skew:.3f} ms"
            )
        elif max_host_offset > 10.0 or camera_skew > 10.0:
            status = "WARN"
            summary = (
                f"相机 NTP 时差偏大：最大主机时差 {max_host_offset:.3f} ms，"
                f"相机间差 {camera_skew:.3f} ms"
            )
        else:
            status = "PASS"
            summary = (
                f"三台相机 NTP 已同步：最大主机时差 {max_host_offset:.3f} ms，"
                f"相机间差 {camera_skew:.3f} ms"
            )

        self.add(
            "time.camera_clocks",
            "时间同步",
            status,
            summary,
            evidence=[
                f"{name}: ntp_offset={offset:+.3f} ms"
                for name, offset in sorted(offsets_by_camera.items())
            ]
            + [
                "measurement=相机内 ntpdate -q 查询对应宿主机链路地址；正值表示相机快于宿主机",
                "actions=未执行时间同步、相机重启或相位调整",
            ],
            impact=(
                "真实 NTP offset 会影响跨设备时间对齐；该数值不同于 HTTP 接口响应时机或消息传输延迟。"
                if status != "PASS"
                else None
            ),
            fixes=(
                ["检查相机 NTP 开关和宿主机 UDP 123 连通性；本诊断不会自动同步或重启相机。"]
                if status != "PASS"
                else []
            ),
        )

    def run(self) -> dict[str, Any]:
        started = time.time()
        self.check_configuration()
        self.check_time()
        self.check_resources()
        self.check_network()
        self.check_containers()
        self.check_dashboard()
        self.check_logs()
        self.check_voice()
        self.check_camera_clocks()
        counts = {status: 0 for status in STATUS_LABEL}
        for finding in self.findings:
            counts[finding.status] += 1
        highest = max((STATUS_RANK[item.status] for item in self.findings), default=0)
        verdict = "FAIL" if highest >= 2 else "WARN" if highest == 1 else "PASS"
        return {
            "schema_version": 1,
            "tool": "insight-system-doctor",
            "generated_at": dt.datetime.now().astimezone().isoformat(),
            "duration_seconds": round(time.time() - started, 3),
            "host": socket.gethostname(),
            "project_root": str(self.root),
            "verdict": verdict,
            "counts": counts,
            "recording_active": bool(self.context.get("recording_active")),
            "findings": [item.as_dict() for item in self.findings],
        }


def repair_camera_timing(
    *,
    root: Path,
    report: Mapping[str, Any],
    runner: Runner | None = None,
) -> RepairOutcome:
    """Synchronize camera clocks and capture phase after explicit authorization."""

    before = next(
        (
            item
            for item in report.get("findings", [])
            if item.get("check_id") == "time.camera_clocks"
        ),
        None,
    )
    before_summary = str(before.get("summary")) if before else "未取得修复前相机时钟结果"
    scope = "scope=修复相机时钟和采集相位"
    if report.get("recording_active"):
        return RepairOutcome(
            finding=Finding(
                check_id="repair.time.camera_timing",
                section="自动修复",
                status="FAIL",
                summary="拒绝修复相机时钟：当前正在录制",
                evidence=[f"before={before_summary}", scope, "action=未执行"],
                impact="校时和采集服务重启会中断正在写入的相机数据。",
                fixes=["停止录制并确认数据落盘完成后，再运行 --repair。"],
            ),
            attempted=False,
            target_check_ids=("time.camera_clocks",),
        )

    identity = os.environ.get("INSIGHT_CAMERA_SSH_IDENTITY")
    password = os.environ.get("INSIGHT_CAMERA_SSH_PASSWORD")
    if not identity and not password:
        return RepairOutcome(
            finding=Finding(
                check_id="repair.time.camera_timing",
                section="自动修复",
                status="FAIL",
                summary="无法修复相机时钟：缺少相机 SSH 凭据",
                evidence=[f"before={before_summary}", scope, "action=未执行"],
                fixes=[
                    "安装 ~/.ssh/insight_camera_ed25519，或设置 INSIGHT_CAMERA_SSH_IDENTITY 后重试。"
                ],
            ),
            attempted=False,
            target_check_ids=("time.camera_clocks",),
        )

    command = [
        sys.executable,
        str(root / "scripts/sync_camera_restart.py"),
        "--json",
    ]
    if identity:
        command.extend(["--identity-file", identity])
    result = (runner or Runner()).run(command, timeout=180.0)
    offsets = parse_camera_ntp_offsets(result.stdout)
    phase = parse_camera_phase_result(result.stdout)
    evidence = [
        f"before={before_summary}",
        scope,
        "action=执行 ntpdate -b、同步重启三路采集服务并测量 10 秒图像时间戳",
    ]
    evidence.extend(_camera_repair_evidence(result.stdout))
    if result.stderr:
        evidence.append(f"stderr={result.stderr.splitlines()[-1][:300]}")

    expected_names = {"insight3_a", "insight3_b", "insight9_a"}
    verified_offsets = set(offsets) == expected_names
    if result.returncode == 0 and verified_offsets and phase and phase["verdict"] == "PASS":
        maximum = max(abs(value) for value in offsets.values())
        spread = max(offsets.values()) - min(offsets.values())
        return RepairOutcome(
            finding=Finding(
                check_id="repair.time.camera_timing",
                section="自动修复",
                status="PASS",
                summary=(
                    f"相机时钟与采集相位修复完成：最大 NTP 时差 {maximum:.3f} ms，"
                    f"相机间差 {spread:.3f} ms，图像最大差 {phase['max_skew_ms']:.3f} ms"
                ),
                evidence=evidence,
            ),
            attempted=True,
            target_check_ids=("time.camera_clocks",),
        )

    if verified_offsets and phase and phase["verdict"] == "FAIL":
        maximum = max(abs(value) for value in offsets.values())
        spread = max(offsets.values()) - min(offsets.values())
        return RepairOutcome(
            finding=Finding(
                check_id="repair.time.camera_timing",
                section="自动修复",
                status="FAIL",
                summary=(
                    f"NTP 校时完成，但采集相位仍未达标：最大 NTP 时差 {maximum:.3f} ms，"
                    f"相机间差 {spread:.3f} ms，图像最大差 {phase['max_skew_ms']:.3f} ms"
                ),
                evidence=evidence,
                impact="设备时钟已对齐，但相机曝光/采集相位仍可能影响严格的跨相机帧配对。",
                fixes=[
                    "检查 Restart timer 和 LPWM 事件差异；排除原因后重新运行 --repair，不要连续盲目重试。"
                ],
            ),
            attempted=True,
            target_check_ids=("time.camera_clocks",),
        )

    detail = _short_error(result)
    return RepairOutcome(
        finding=Finding(
            check_id="repair.time.camera_timing",
            section="自动修复",
            status="FAIL",
            summary="相机时钟与采集相位修复命令失败",
            evidence=evidence + [f"exit={result.returncode}", f"error={detail}"],
            impact="无法确认三台相机是否完成校时和同步采集服务重启。",
            fixes=["按上方错误检查 SSH、NTP、相机服务和 Dashboard 状态后再重试。"],
        ),
        attempted=True,
        target_check_ids=("time.camera_clocks",),
    )


def _problem_findings(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("check_id")): item
        for item in report.get("findings", [])
        if item.get("status") in {"WARN", "FAIL"}
    }


def _bounded_command_evidence(result: CommandResult, *, limit: int = 16) -> list[str]:
    lines = [line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()]
    if len(lines) > limit:
        half = limit // 2
        lines = lines[:half] + [f"... omitted {len(lines) - limit} lines ..."] + lines[-half:]
    return lines


def _run_command_repair(
    *,
    action_id: str,
    summary: str,
    command: Sequence[str],
    target_check_ids: Sequence[str],
    before: Mapping[str, Mapping[str, Any]],
    runner: Runner,
    timeout: float,
    failure_fix: str = "根据命令输出处理权限或服务错误后，再运行 --repair。",
) -> RepairOutcome:
    result = runner.run(command, timeout=timeout)
    evidence = [
        "targets=" + ", ".join(target_check_ids),
        "command=" + " ".join(command),
    ]
    evidence.extend(
        f"before[{check_id}]={before[check_id].get('summary')}"
        for check_id in target_check_ids
        if check_id in before
    )
    evidence.extend(_bounded_command_evidence(result))
    if result.returncode == 0:
        finding = Finding(
            check_id=action_id,
            section="自动修复",
            status="PASS",
            summary=f"{summary}命令执行完成，等待复检",
            evidence=evidence,
        )
    else:
        finding = Finding(
            check_id=action_id,
            section="自动修复",
            status="FAIL",
            summary=f"{summary}命令失败",
            evidence=evidence + [f"exit={result.returncode}", f"error={_short_error(result)}"],
            fixes=[failure_fix],
        )
    return RepairOutcome(
        finding=finding,
        attempted=True,
        target_check_ids=tuple(target_check_ids),
    )


def repair_system(
    *,
    root: Path,
    report: Mapping[str, Any],
    runner: Runner | None = None,
) -> list[RepairOutcome]:
    """Run deterministic repair handlers for the problems found in a report."""

    problems = _problem_findings(report)
    all_findings = {
        str(item.get("check_id")): item
        for item in report.get("findings", [])
    }
    camera_timing_available = "time.camera_clocks" in all_findings
    if not problems and not camera_timing_available:
        return [
            RepairOutcome(
                finding=Finding(
                    check_id="repair.none",
                    section="自动修复",
                    status="PASS",
                    summary="本轮诊断没有需要修复的故障或警告",
                ),
                attempted=False,
            )
        ]
    if report.get("recording_active"):
        safety_targets = set(problems)
        if camera_timing_available:
            safety_targets.add("time.camera_clocks")
        return [
            RepairOutcome(
                finding=Finding(
                    check_id="repair.safety",
                    section="自动修复",
                    status="FAIL",
                    summary="当前正在录制，已拒绝全部自动修复",
                    evidence=["targets=" + ", ".join(sorted(safety_targets)), "actions=未执行"],
                    impact="服务重启、网络参数变更或相机校时可能中断正在写入的数据。",
                    fixes=["停止录制并确认数据落盘完成后，再运行 --repair。"],
                ),
                attempted=False,
                target_check_ids=tuple(sorted(safety_targets)),
            )
        ]

    command_runner = runner or Runner()
    compose_file = str(root / "docker-compose.yml")
    outcomes: list[RepairOutcome] = []
    handled: set[str] = set()

    network_ids = tuple(
        check_id
        for check_id in ("network.kernel_tuning", "network.rps", "network.drop_sample")
        if check_id in problems
    )
    if network_ids:
        outcomes.append(
            _run_command_repair(
                action_id="repair.host_setup",
                summary="主机 DDS/RPS 配置修复",
                command=["sudo", "-n", str(root / "deploy/host_setup.sh")],
                target_check_ids=network_ids,
                before=problems,
                runner=command_runner,
                timeout=120.0,
                failure_fix="在交互终端先运行 sudo -v 取得主机权限，再重新运行 ./scripts/system_doctor.sh --repair。",
            )
        )
        handled.update(network_ids)

    if "time.host_ntp" in problems:
        outcomes.append(
            _run_command_repair(
                action_id="repair.time.host_ntp",
                summary="宿主机 chrony 服务修复",
                command=["sudo", "-n", "systemctl", "restart", "chrony"],
                target_check_ids=("time.host_ntp",),
                before=problems,
                runner=command_runner,
                timeout=30.0,
                failure_fix="在交互终端先运行 sudo -v，并确认 chrony.service 已安装后重试。",
            )
        )
        handled.add("time.host_ntp")

    core_containers_bad = "containers.required" in problems
    if core_containers_bad:
        outcomes.append(
            _run_command_repair(
                action_id="repair.containers.required",
                summary="核心容器启动修复",
                command=[
                    "docker",
                    "compose",
                    "-f",
                    compose_file,
                    "up",
                    "-d",
                    "insight-dashboard",
                    "superglue-inference",
                    "insight9-sparse-mapper",
                    "insight3-global-localizer",
                ],
                target_check_ids=("containers.required",),
                before=problems,
                runner=command_runner,
                timeout=180.0,
            )
        )
        handled.add("containers.required")

    container_time_bad = "containers.start_time" in problems
    if container_time_bad:
        outcomes.append(
            _run_command_repair(
                action_id="repair.containers.start_time",
                summary="容器启动时间元数据修复",
                command=[
                    "docker",
                    "compose",
                    "-f",
                    compose_file,
                    "up",
                    "-d",
                    "--force-recreate",
                    "superglue-inference",
                    "insight9-sparse-mapper",
                    "insight3-global-localizer",
                ],
                target_check_ids=("containers.start_time",),
                before=problems,
                runner=command_runner,
                timeout=180.0,
            )
        )
        handled.add("containers.start_time")

    dashboard_ids = tuple(
        check_id
        for check_id in problems
        if check_id in {"dashboard.http", "camera.api", "media.webrtc_worker", "media.hardware"}
        or check_id.startswith("camera.")
    )
    if dashboard_ids and not core_containers_bad:
        outcomes.append(
            _run_command_repair(
                action_id="repair.dashboard.runtime",
                summary="Dashboard 与图像运行态修复",
                command=[
                    "docker",
                    "compose",
                    "-f",
                    compose_file,
                    "up",
                    "-d",
                    "--force-recreate",
                    "insight-dashboard",
                ],
                target_check_ids=dashboard_ids,
                before=problems,
                runner=command_runner,
                timeout=120.0,
            )
        )
        handled.update(dashboard_ids)

    if "mapping.services" in problems and not core_containers_bad and not container_time_bad:
        outcomes.append(
            _run_command_repair(
                action_id="repair.mapping.runtime",
                summary="建图定位服务运行态修复",
                command=[
                    "docker",
                    "compose",
                    "-f",
                    compose_file,
                    "up",
                    "-d",
                    "--force-recreate",
                    "superglue-inference",
                    "insight9-sparse-mapper",
                    "insight3-global-localizer",
                ],
                target_check_ids=("mapping.services",),
                before=problems,
                runner=command_runner,
                timeout=180.0,
            )
        )
        handled.add("mapping.services")

    if "voice.service" in problems:
        outcomes.append(
            _run_command_repair(
                action_id="repair.voice.service",
                summary="语音控制服务修复",
                command=["systemctl", "--user", "restart", "insight-voice-control.service"],
                target_check_ids=("voice.service",),
                before=problems,
                runner=command_runner,
                timeout=30.0,
            )
        )
        handled.add("voice.service")

    runtime_action_ids = {
        "repair.containers.required",
        "repair.containers.start_time",
        "repair.dashboard.runtime",
        "repair.mapping.runtime",
    }
    if any(outcome.finding.check_id in runtime_action_ids for outcome in outcomes):
        time.sleep(8.0)

    if camera_timing_available:
        outcomes.append(
            repair_camera_timing(root=root, report=report, runner=command_runner)
        )
        if "time.camera_clocks" in problems:
            handled.add("time.camera_clocks")

    manual_ids = sorted(set(problems) - handled)
    if manual_ids:
        outcomes.append(
            RepairOutcome(
                finding=Finding(
                    check_id="repair.manual_required",
                    section="自动修复",
                    status="INFO",
                    summary=f"{len(manual_ids)} 个问题没有安全、无歧义的自动修复",
                    evidence=[
                        f"{check_id}: {problems[check_id].get('summary')}"
                        for check_id in manual_ids
                    ],
                    impact="这些问题涉及现场选择、物理操作、数据删除或根因分析，已保留原诊断建议。",
                ),
                attempted=False,
                target_check_ids=tuple(manual_ids),
            )
        )
    return outcomes


def reconcile_repair_outcomes(
    outcomes: Sequence[RepairOutcome],
    report: Mapping[str, Any],
) -> None:
    """Attach post-check evidence and fail successful commands that did not fix state."""

    post = {
        str(item.get("check_id")): item
        for item in report.get("findings", [])
    }
    for outcome in outcomes:
        if not outcome.attempted or not outcome.target_check_ids:
            continue
        statuses = {
            check_id: str(post.get(check_id, {}).get("status") or "MISSING")
            for check_id in outcome.target_check_ids
        }
        outcome.finding.evidence.append(
            "postcheck=" + ", ".join(f"{key}:{value}" for key, value in statuses.items())
        )
        remaining = {
            key: value for key, value in statuses.items() if value not in {"PASS", "SKIP"}
        }
        if outcome.finding.status == "PASS" and remaining:
            outcome.finding.status = "FAIL" if "FAIL" in remaining.values() else "WARN"
            outcome.finding.summary = (
                outcome.finding.summary.removesuffix("，等待复检")
                + "，但复检仍异常："
                + ", ".join(f"{key}={value}" for key, value in remaining.items())
            )
            outcome.finding.fixes = ["查看对应复检项的新证据，处理根因后再运行 --repair。"]
        elif outcome.finding.status == "PASS":
            outcome.finding.summary = outcome.finding.summary.removesuffix("，等待复检") + "，复检通过"


def _append_finding(report: dict[str, Any], finding: Finding) -> None:
    report["findings"].append(finding.as_dict())
    counts = {status: 0 for status in STATUS_LABEL}
    for item in report["findings"]:
        counts[item["status"]] += 1
    highest = max(
        (STATUS_RANK[item["status"]] for item in report["findings"]),
        default=0,
    )
    report["counts"] = counts
    report["verdict"] = "FAIL" if highest >= 2 else "WARN" if highest == 1 else "PASS"


def _summary_line(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    return (
        f"结论: {STATUS_LABEL[report['verdict']]}  "
        f"故障 {counts['FAIL']} / 警告 {counts['WARN']} / "
        f"正常 {counts['PASS']} / 信息或跳过 {counts['INFO'] + counts['SKIP']}"
    )


def render_report(
    report: Mapping[str, Any],
    *,
    verbose: bool,
    color: bool,
    output_path: Path | None = None,
) -> str:
    colors = {
        "PASS": "\033[32m",
        "INFO": "\033[36m",
        "SKIP": "\033[90m",
        "WARN": "\033[33m",
        "FAIL": "\033[31m",
    }
    reset = "\033[0m"

    def badge(status: str) -> str:
        label = f"[{STATUS_LABEL[status]}]"
        return f"{colors[status]}{label}{reset}" if color else label

    summary = _summary_line(report)
    lines = [
        "Insight Capture 系统深度诊断",
        f"时间: {report['generated_at']}  主机: {report['host']}",
        summary,
        "",
    ]
    last_section = None
    for finding in report["findings"]:
        if finding["section"] != last_section:
            if last_section is not None:
                lines.append("")
            lines.append(f"== {finding['section']} ==")
            last_section = finding["section"]
        lines.append(f"{badge(finding['status'])} {finding['summary']}  ({finding['check_id']})")
        show_evidence = verbose or finding["status"] in {"WARN", "FAIL", "INFO"}
        if show_evidence:
            for evidence in finding["evidence"]:
                lines.append(f"  证据: {evidence}")
        if finding.get("impact") and finding["status"] in {"WARN", "FAIL", "INFO"}:
            lines.append(f"  影响: {finding['impact']}")
        if finding["status"] in {"WARN", "FAIL", "INFO"}:
            for index, fix in enumerate(finding["fixes"], start=1):
                lines.append(f"  修复 {index}: {fix}")

    lines.extend(["", f"诊断耗时: {report['duration_seconds']:.1f} 秒"])
    if report.get("recording_active"):
        lines.append("安全提示: 当前正在录制；不要执行任何重启、重建容器、重挂载或相机断电操作。")
    if output_path is not None:
        lines.append(f"完整 JSON 已保存：{output_path}")
    lines.extend(["", summary])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="深度检查主机、相机、ROS、容器、录制盘和 Dashboard，并给出证据与修复建议。"
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8765", help="Dashboard API base URL")
    parser.add_argument("--log-since", default="30m", help="Docker log window, for example 30m or 2h")
    parser.add_argument("--sample-seconds", type=float, default=2.0, help="Network drop counter sampling window")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the human report")
    parser.add_argument("--output", type=Path, help="Also save the complete JSON report to this path")
    parser.add_argument("--verbose", action="store_true", help="Show evidence for successful checks too")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("--fail-on-warning", action="store_true", help="Return nonzero when warnings exist")
    parser.add_argument(
        "--repair",
        dest="repair",
        action="store_true",
        help="Repair detected problems with registered safe actions, then run diagnostics again",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    workflow_started = time.time()
    args = build_parser().parse_args(argv)
    if args.sample_seconds < 0 or args.sample_seconds > 30:
        print("ERROR: --sample-seconds must be between 0 and 30", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[2]
    doctor = SystemDoctor(
        root=root,
        api_url=args.api_url,
        log_since=args.log_since,
        sample_seconds=args.sample_seconds,
    )
    report = doctor.run()
    if args.repair:
        outcomes = repair_system(root=root, report=report)
        attempted = any(outcome.attempted for outcome in outcomes)
        if attempted:
            report = SystemDoctor(
                root=root,
                api_url=args.api_url,
                log_since=args.log_since,
                sample_seconds=args.sample_seconds,
            ).run()
            reconcile_repair_outcomes(outcomes, report)
        report["repair_requested"] = True
        report["repair_attempted"] = attempted
        report["repair_action_count"] = sum(outcome.attempted for outcome in outcomes)
        for outcome in outcomes:
            _append_finding(report, outcome.finding)
        report["duration_seconds"] = round(time.time() - workflow_started, 3)
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"ERROR: cannot write {args.output}: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            render_report(
                report,
                verbose=args.verbose,
                color=not args.no_color and sys.stdout.isatty(),
                output_path=args.output,
            )
        )
    if report["counts"]["FAIL"]:
        return 2
    if args.fail_on_warning and report["counts"]["WARN"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
