"""Deterministic offline voice commands and reply selection."""

import re
from typing import Iterable, Optional


LOCAL_COMMAND_ALIASES = {
    "recording_start": ("开始录制", "开始录像", "开始采集"),
    "recording_stop": ("结束录制", "停止录制", "结束录像", "停止录像", "结束采集", "停止采集"),
    "calibration_start": ("开始校准", "重新校准", "重置校准"),
    "capture_check": ("检查相机", "开始检测", "位置检测", "检测相机"),
    "capture_reference": ("设置检测位", "记录检测位", "保存检测位"),
    "system_status": ("系统状态", "检查系统", "数采状态"),
    "take_reject": ("本条作废", "这条作废", "作废本条"),
    "task_cup_stacking_start": (
        "开始任务叠杯子",
        "开始叠杯子任务",
        "切换任务叠杯子",
        "切换到叠杯子",
    ),
    "task_status": (
        "当前任务",
        "任务状态",
        "查询当前任务",
        "当前任务多少条",
        "当前任务有多少条",
        "叠杯子多少条",
        "叠杯子录了多少条",
    ),
    "task_end": ("结束当前任务", "结束任务", "完成当前任务"),
}

LOCAL_COMMAND_ENDPOINTS = {
    "recording_start": "/api/automation/recording/start",
    "recording_stop": "/api/automation/recording/stop",
    "calibration_start": "/api/mapping/reset",
    "capture_check": "/api/capture-check/run",
    "capture_reference": "/api/capture-check/reference",
    "system_status": "/api/system/status",
    "take_reject": "/api/takes/current/reject",
    "task_cup_stacking_start": "/api/tasks/cup_stacking/activate",
    "task_status": "/api/tasks/current",
    "task_end": "/api/tasks/current/end",
}

LOCAL_COMMAND_REPLY_KEYS = {
    "recording_start": "recording_started",
    "recording_stop": "recording_stopped",
    "calibration_start": "calibration_started",
    "capture_check": "capture_check_not_ready",
    "capture_reference": "capture_reference_saved",
    "system_status": "dynamic_reply",
    "take_reject": "dynamic_reply",
    "task_cup_stacking_start": "dynamic_reply",
    "task_status": "dynamic_reply",
    "task_end": "dynamic_reply",
}


class LocalCommandFailure(RuntimeError):
    """A deterministic command failure with operator-facing speech."""

    def __init__(self, message: str, speech: Optional[str] = None) -> None:
        super().__init__(message)
        self.speech = str(speech or "").strip()


def normalize_transcript(text: object) -> str:
    return " ".join(re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]+", str(text or "").lower()))


def wake_word_detected(text: object, wake_phrases: Iterable[str]) -> bool:
    transcript = normalize_transcript(text)
    if not transcript:
        return False
    compact = transcript.replace(" ", "")
    return any(
        (normalized := normalize_transcript(phrase))
        and (normalized in transcript.split() or normalized.replace(" ", "") == compact)
        for phrase in wake_phrases
    )


def match_local_command(text: object) -> Optional[str]:
    normalized = normalize_transcript(text).replace(" ", "")
    for prefix in ("请帮我", "帮我", "请"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    for suffix in ("一下", "吧"):
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    return next((action for action, aliases in LOCAL_COMMAND_ALIASES.items() if normalized in aliases), None)


def calibration_is_complete(payload: object) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("statuses"), dict):
        return False
    statuses = payload["statuses"]
    return all(isinstance(statuses.get(name), dict) and bool(statuses[name].get("localized")) for name in ("insight3_a", "insight3_b"))


def capture_check_reply_key(payload: object, *, reference: bool = False) -> str:
    if not isinstance(payload, dict):
        return "capture_check_not_ready"
    state = str(payload.get("state") or "not_ready")
    if reference and state == "reference_saved":
        return "capture_reference_saved"
    return {"pass": "capture_check_pass", "retry": "capture_check_retry", "recalibrate": "capture_check_recalibrate", "no_reference": "capture_check_no_reference"}.get(state, "capture_check_not_ready")
