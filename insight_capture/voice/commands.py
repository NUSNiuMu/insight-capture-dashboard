"""Deterministic offline voice commands and reply selection."""

import re
from typing import Iterable, Optional


LOCAL_COMMAND_ALIASES = {
    "recording_start": ("开始录制", "开始录像", "开始采集"),
    "vio_calibration_recording_start": (
        "录制校准模式",
        "开始录制校准模式",
        "开始校准录制",
    ),
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
    "vio_calibration_recording_start": (
        "/api/automation/recording/vio-calibration/start"
    ),
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
    "vio_calibration_recording_start": "vio_calibration_recording_started",
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


def capture_check_speech(payload: object) -> Optional[str]:
    """Describe failed station checks with operator-facing camera names."""
    if not isinstance(payload, dict):
        return None
    state = str(payload.get("state") or "not_ready")
    if state == "pass":
        return None
    if state == "no_reference":
        return "还没有检测位基准。请放好三台相机。然后说设置检测位。"

    comparisons = payload.get("comparisons")
    comparisons = comparisons if isinstance(comparisons, dict) else {}
    camera_names = {
        "insight3_a": "右手相机",
        "insight3_b": "左手相机",
        "insight9_a": "头部相机",
    }
    messages = []
    needs_recalibration = state == "recalibrate"
    for camera_id in ("insight3_a", "insight3_b", "insight9_a"):
        comparison = comparisons.get(camera_id)
        if not isinstance(comparison, dict):
            continue
        camera_state = str(comparison.get("state") or "pass")
        if camera_state == "pass":
            continue
        name = camera_names[camera_id]
        if camera_id == "insight9_a":
            reason = str(comparison.get("reason") or "").lower()
            if "stale" in reason:
                messages.append(
                    f"{name}没有获得新的地图闭环。请对准检测位方向，并小范围缓慢扫动。"
                )
            elif camera_state == "recalibrate":
                messages.append(f"{name}地图偏差过大。")
            else:
                messages.append(f"{name}地图闭环没有通过。请重新扫视工作区。")
        elif camera_state == "recalibrate":
            messages.append(f"{name}位置偏差过大。")
        else:
            messages.append(f"{name}没有回到检测位。请重新归位。")
        needs_recalibration = needs_recalibration or camera_state == "recalibrate"

    if not messages:
        reasons = [
            str(reason).lower()
            for reason in (payload.get("reasons") or [])
            if str(reason).strip()
        ]
        if any("stationary" in reason for reason in reasons):
            messages.append("相机还没有静止。请放稳后重新检测。")
        elif any("offline" in reason or "localized" in reason for reason in reasons):
            messages.append("相机定位服务没有准备好。请检查定位状态。")
        elif any("stale" in reason for reason in reasons):
            messages.append(
                "头部相机闭环已经过期。请对准检测位方向，并小范围缓慢扫动。"
            )
        else:
            messages.append("检测条件没有准备好。请检查相机位置和定位状态。")

    ending = (
        "需要重新校准。请说开始校准。"
        if needs_recalibration
        else "暂时不需要重新校准。请调整后再次检查。"
    )
    return "检测没有通过。" + "".join(messages) + ending
