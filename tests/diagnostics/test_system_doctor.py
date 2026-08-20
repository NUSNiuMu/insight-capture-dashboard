import json

from insight_capture.diagnostics.system_doctor import (
    _error_lines,
    _parse_compose_rows,
    parse_chrony_tracking,
    render_report,
)


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

    rendered = render_report(report, verbose=False, color=False)

    assert "[故障] insight3_a 无新鲜图像" in rendered
    assert "证据: input_age_sec=None" in rendered
    assert "修复 1: 检查 USB 线。" in rendered
    assert "当前正在录制" in rendered
