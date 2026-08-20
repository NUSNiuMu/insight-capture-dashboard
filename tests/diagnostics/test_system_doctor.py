import json
from pathlib import Path

from insight_capture.diagnostics.system_doctor import (
    CommandResult,
    SystemDoctor,
    _error_lines,
    _existing_staging_entries,
    _parse_compose_rows,
    build_parser,
    parse_chrony_tracking,
    parse_camera_ntp_offsets,
    parse_camera_phase_result,
    repair_camera_timing,
    repair_system,
    reconcile_repair_outcomes,
    render_report,
)


ROOT = Path(__file__).resolve().parents[2]


def test_parse_chrony_tracking_preserves_signed_offset() -> None:
    tracking = parse_chrony_tracking(
        """Reference ID    : 162.159.200.123
Stratum         : 4
System time     : 0.000014 seconds slow of NTP time
Last offset     : -0.000002 seconds
Leap status     : Normal
"""
    )

    assert tracking["Reference ID"] == "162.159.200.123"
    assert tracking["stratum"] == 4
    assert tracking["system_offset_seconds"] == -0.000014
    assert tracking["last_offset_seconds"] == -0.000002


def test_parse_camera_ntp_offsets_ignores_estimated_spread() -> None:
    offsets = parse_camera_ntp_offsets(
        """Current NTP offsets
  insight3_a     +0.412 ms
  insight3_b     -1.203 ms
  insight9_a     +0.067 ms
  estimated spread 1.615 ms
"""
    )

    assert offsets == {
        "insight3_a": 0.412,
        "insight3_b": -1.203,
        "insight9_a": 0.067,
    }


def test_parse_camera_phase_result() -> None:
    assert parse_camera_phase_result(
        "Result: FAIL (max observed skew 16.664 ms, limit 10.000 ms)"
    ) == {
        "verdict": "FAIL",
        "max_skew_ms": 16.664,
        "limit_ms": 10.0,
    }


def test_repair_flag_is_available() -> None:
    assert build_parser().parse_args(["--repair"]).repair is True


class StubRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], float]] = []

    def run(self, argv: list[str], *, timeout: float = 8.0) -> CommandResult:
        self.calls.append((list(argv), timeout))
        return self.result


def _camera_clock_report(*, recording_active: bool) -> dict:
    return {
        "recording_active": recording_active,
        "findings": [
            {
                "check_id": "time.camera_clocks",
                "status": "FAIL",
                "summary": "相机 NTP 时差过大",
            }
        ],
    }


def test_repair_camera_timing_refuses_during_recording() -> None:
    runner = StubRunner(CommandResult([], 0, "", "", 0))

    outcome = repair_camera_timing(
        root=ROOT,
        report=_camera_clock_report(recording_active=True),
        runner=runner,
    )

    assert outcome.attempted is False
    assert outcome.finding.status == "FAIL"
    assert "正在录制" in outcome.finding.summary
    assert runner.calls == []


def test_repair_camera_timing_reports_phase_failure(monkeypatch) -> None:
    monkeypatch.setenv("INSIGHT_CAMERA_SSH_IDENTITY", "/tmp/camera-key")
    output = """Current NTP offsets
  insight3_a     -2.764 ms
  insight3_b    -69.463 ms
  insight9_a    -31.029 ms
Post-sync NTP offsets
  insight3_a     -0.146 ms
  insight3_b     -0.307 ms
  insight9_a     -0.015 ms
Final NTP offsets
  insight3_a     -0.444 ms
  insight3_b     -0.357 ms
  insight9_a     -0.348 ms
Result: FAIL (max observed skew 16.664 ms, limit 10.000 ms)
"""
    runner = StubRunner(CommandResult([], 2, output, "", 90000))

    outcome = repair_camera_timing(
        root=ROOT,
        report=_camera_clock_report(recording_active=False),
        runner=runner,
    )

    assert outcome.attempted is True
    assert outcome.finding.status == "FAIL"
    assert "最大 NTP 时差 0.444 ms" in outcome.finding.summary
    assert "图像最大差 16.664 ms" in outcome.finding.summary
    assert runner.calls[0][1] == 180.0
    assert "--identity-file" in runner.calls[0][0]


def test_repair_camera_timing_reports_success(monkeypatch) -> None:
    monkeypatch.setenv("INSIGHT_CAMERA_SSH_IDENTITY", "/tmp/camera-key")
    output = """Final NTP offsets
  insight3_a     +0.112 ms
  insight3_b     +0.103 ms
  insight9_a     +0.022 ms
Result: PASS (max observed skew 3.665 ms, limit 10.000 ms)
"""
    runner = StubRunner(CommandResult([], 0, output, "", 90000))

    outcome = repair_camera_timing(
        root=ROOT,
        report=_camera_clock_report(recording_active=False),
        runner=runner,
    )

    assert outcome.attempted is True
    assert outcome.finding.status == "PASS"
    assert "最大 NTP 时差 0.112 ms" in outcome.finding.summary
    assert "图像最大差 3.665 ms" in outcome.finding.summary


def test_repair_system_runs_registered_actions_and_lists_manual_work() -> None:
    report = {
        "recording_active": False,
        "findings": [
            {"check_id": "network.rps", "status": "FAIL", "summary": "RPS 未启用"},
            {"check_id": "voice.service", "status": "WARN", "summary": "语音未运行"},
            {
                "check_id": "recording.storage",
                "status": "WARN",
                "summary": "录制正在使用备用存储",
            },
        ],
    }
    runner = StubRunner(CommandResult([], 0, "repair complete", "", 100))

    outcomes = repair_system(root=ROOT, report=report, runner=runner)

    assert [outcome.finding.check_id for outcome in outcomes] == [
        "repair.host_setup",
        "repair.voice.service",
        "repair.manual_required",
    ]
    assert runner.calls[0][0] == [
        "sudo",
        "-n",
        str(ROOT / "deploy/host_setup.sh"),
    ]
    assert runner.calls[1][0] == [
        "systemctl",
        "--user",
        "restart",
        "insight-voice-control.service",
    ]
    assert "recording.storage" in outcomes[-1].finding.evidence[0]


def test_repair_system_preserves_explicit_camera_calibration(monkeypatch) -> None:
    monkeypatch.setenv("INSIGHT_CAMERA_SSH_IDENTITY", "/tmp/camera-key")
    report = {
        "recording_active": False,
        "findings": [
            {
                "check_id": "time.camera_clocks",
                "status": "PASS",
                "summary": "三台相机 NTP 已同步",
            },
        ],
    }
    output = """Final NTP offsets
  insight3_a     +0.112 ms
  insight3_b     +0.103 ms
  insight9_a     +0.022 ms
Result: PASS (max observed skew 3.665 ms, limit 10.000 ms)
"""
    runner = StubRunner(CommandResult([], 0, output, "", 90000))

    outcomes = repair_system(root=ROOT, report=report, runner=runner)

    assert [outcome.finding.check_id for outcome in outcomes] == [
        "repair.time.camera_timing"
    ]
    assert outcomes[0].finding.status == "PASS"
    assert "sync_camera_restart.py" in runner.calls[0][0][1]


def test_repair_system_refuses_all_actions_while_recording() -> None:
    report = {
        "recording_active": True,
        "findings": [
            {"check_id": "network.rps", "status": "FAIL", "summary": "RPS 未启用"},
            {"check_id": "voice.service", "status": "WARN", "summary": "语音未运行"},
        ],
    }
    runner = StubRunner(CommandResult([], 0, "", "", 0))

    outcomes = repair_system(root=ROOT, report=report, runner=runner)

    assert len(outcomes) == 1
    assert outcomes[0].finding.check_id == "repair.safety"
    assert outcomes[0].finding.status == "FAIL"
    assert runner.calls == []


def test_reconcile_marks_successful_command_failed_when_postcheck_fails() -> None:
    report = {
        "recording_active": False,
        "findings": [
            {"check_id": "network.rps", "status": "FAIL", "summary": "RPS 未启用"},
        ],
    }
    runner = StubRunner(CommandResult([], 0, "repair complete", "", 100))
    outcomes = repair_system(root=ROOT, report=report, runner=runner)

    reconcile_repair_outcomes(outcomes, report)

    assert outcomes[0].finding.status == "FAIL"
    assert "复检仍异常" in outcomes[0].finding.summary
    assert outcomes[0].finding.evidence[-1] == "postcheck=network.rps:FAIL"


def test_host_setup_permission_failure_has_concrete_fix() -> None:
    report = {
        "recording_active": False,
        "findings": [
            {"check_id": "network.rps", "status": "FAIL", "summary": "RPS 未启用"},
        ],
    }
    runner = StubRunner(CommandResult([], 1, "", "sudo: a password is required", 10))

    outcomes = repair_system(root=ROOT, report=report, runner=runner)

    assert outcomes[0].finding.status == "FAIL"
    assert outcomes[0].finding.fixes == [
        "在交互终端先运行 sudo -v 取得主机权限，再重新运行 ./scripts/system_doctor.sh --repair。"
    ]


def test_staging_check_uses_current_disk_state(tmp_path: Path) -> None:
    staging = tmp_path / "rosbags/_staging"
    staging.mkdir(parents=True)
    interrupted = staging / "insight_record_interrupted"
    interrupted.mkdir()

    assert _existing_staging_entries([staging]) == [interrupted]

    interrupted.rmdir()
    assert _existing_staging_entries([staging]) == []


def test_parse_compose_rows_accepts_array_and_json_lines() -> None:
    rows = [{"Service": "insight-dashboard", "State": "running"}]
    assert _parse_compose_rows(json.dumps(rows)) == rows
    assert _parse_compose_rows("\n".join(json.dumps(item) for item in rows)) == rows


def test_error_lines_excludes_zero_failure_counters() -> None:
    lines = _error_lines(
        [
            "worker failed to open socket",
            "failure_count=0",
            "0 errors detected",
            "Traceback (most recent call last):",
        ]
    )

    assert lines == [
        "worker failed to open socket",
        "Traceback (most recent call last):",
    ]


def test_error_lines_excludes_unused_jetson_gstreamer_plugin_scan_noise() -> None:
    lines = _error_lines(
        [
            "GStreamer-WARNING: Failed to load plugin '/usr/lib/gstreamer-1.0/libgstnvvideosinks.so': libGLESv2.so.2: cannot open shared object file",
            "GStreamer-WARNING: Failed to load plugin '/usr/lib/gstreamer-1.0/libgstnveglglessink.so': libGLESv2.so.2: cannot open shared object file",
            "GStreamer-WARNING: Failed to load plugin '/usr/lib/gstreamer-1.0/libgstnvarguscamerasrc.so': libGLESv2.so.2: cannot open shared object file",
            "GStreamer-WARNING: Failed to load plugin '/usr/lib/gstreamer-1.0/libgstwebrtc.so': missing required library",
        ]
    )

    assert lines == [
        "GStreamer-WARNING: Failed to load plugin '/usr/lib/gstreamer-1.0/libgstwebrtc.so': missing required library"
    ]


def test_storage_fallback_is_counted_only_by_dedicated_check(monkeypatch) -> None:
    doctor = SystemDoctor(
        root=ROOT,
        api_url="http://127.0.0.1:8765",
        log_since="30m",
        sample_seconds=0,
        runner=StubRunner(CommandResult([], 1, "", "not mounted", 0)),
    )
    payloads = {
        "/healthz": {"ok": True, "fake_pose": False},
        "/api/system/status": {
            "preflight": {
                "failures": [],
                "warnings": [
                    {
                        "code": "storage_fallback",
                        "message": "using fallback storage",
                        "details": {"reason": "required source missing"},
                    }
                ],
                "storage": {"free_bytes": 1024},
                "topics": {"missing": []},
            }
        },
        "/api/cameras": {"cameras": []},
        "/api/recording/status": {
            "recording": False,
            "storage": {
                "using_fallback": True,
                "configured_path": "/mnt/insight-recordings",
                "active_path": "/workspaces/insight_capture/rosbags",
                "required_source": "/dev/sda1",
                "mounted_source": "/dev/nvme1n1p1",
                "fallback_reason": "required source missing",
            },
        },
        "/api/images/capabilities": {},
        "/api/mapping": {},
    }
    monkeypatch.setattr(
        doctor,
        "fetch_json",
        lambda path, timeout=8.0: (payloads[path], None),
    )

    doctor.check_dashboard()

    findings = {finding.check_id: finding for finding in doctor.findings}
    assert findings["dashboard.preflight"].status == "PASS"
    assert "已合并" in findings["dashboard.preflight"].summary
    assert findings["recording.storage"].status == "WARN"


def test_render_report_shows_actionable_failure_details() -> None:
    report = {
        "generated_at": "2026-08-20T12:00:00+08:00",
        "host": "capture-host",
        "verdict": "FAIL",
        "counts": {"PASS": 0, "INFO": 0, "SKIP": 0, "WARN": 0, "FAIL": 1},
        "duration_seconds": 1.25,
        "recording_active": True,
        "findings": [
            {
                "check_id": "camera.insight3_a",
                "section": "相机数据",
                "status": "FAIL",
                "summary": "insight3_a 无新鲜图像",
                "evidence": ["input_age_sec=None"],
                "impact": "该相机无法录制。",
                "fixes": ["检查 USB 线。"],
            }
        ],
    }

    rendered = render_report(
        report,
        verbose=False,
        color=False,
        output_path=Path("/tmp/system-doctor.json"),
    )

    assert "[故障] insight3_a 无新鲜图像" in rendered
    assert "证据: input_age_sec=None" in rendered
    assert "修复 1: 检查 USB 线。" in rendered
    assert "当前正在录制" in rendered
    assert "完整 JSON 已保存：/tmp/system-doctor.json" in rendered
    summary = "结论: 故障  故障 1 / 警告 0 / 正常 0 / 信息或跳过 0"
    assert rendered.count(summary) == 2
    assert rendered.endswith(summary)


def test_shell_entrypoint_enables_verbose_timestamped_report() -> None:
    script = (ROOT / "scripts/system_doctor.sh").read_text(encoding="utf-8")

    assert "--verbose" in script
    assert "--output" in script
    assert "date +%Y%m%d_%H%M%S" in script
    assert "insight_camera_ed25519" in script
    assert "INSIGHT_CAMERA_SSH_IDENTITY" in script
    assert "INSIGHT_CAMERA_SSH_PASSWORD" in script
    assert "sudo -v" in script
    assert "不会保存" in script
    assert '"$@"' in script
