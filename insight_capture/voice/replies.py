"""Offline operator replies and speech text cleanup."""

import re
from typing import Iterable


CANNED_REPLIES = {
    "recording_starting": "初始化录制中，请稍等。",
    "recording_started": "录制已经开始。",
    "recording_stopping": "正在结束录制。",
    "recording_stopped": "录制已经结束。请把左右手相机放回检测位。然后说检查相机。检测时请把头部相机对准检测位方向，并小范围缓慢扫动。",
    "calibration_started": "校准已经开始。",
    "calibration_completed": "校准完成。",
    "capture_check_started": "开始检测。请把头部相机对准检测位方向，并小范围缓慢扫动。",
    "capture_reference_saved": "检测位已经记录。头部相机地图基准已经记录。",
    "capture_check_pass": "检测通过。三台相机状态正常。可以开始下一次采集。",
    "capture_check_retry": "检测没有通过。请把双手相机放回检测位。请把头部相机对准检测位方向，并小范围缓慢扫动。然后再次检查。",
    "capture_check_recalibrate": "相机偏差过大。需要重新校准。请说开始校准。",
    "capture_check_not_ready": "检测条件没有准备好。请确认相机已经静止。请检查定位状态。",
    "capture_check_no_reference": "还没有检测位基准。请放好三台相机。然后说设置检测位。",
    "recording_already_active": "当前已经在录制。",
    "command_failed": "指令执行失败，请检查数采服务。",
}


def speech_text(text: object, max_chars: int = 240) -> str:
    value = str(text or "")
    value = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[\[tts:[^\]]+]]", "", value).replace("[[/tts:text]]", "").replace("[[tts:text]]", "")
    value = re.sub(r"[`*_#>|]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"[；;]+", "。", value)
    value = re.sub(r"。{2,}", "。", value)
    return value if len(value) <= max_chars else value[:max(1, max_chars - 1)].rstrip("，。！？；：,.!?;:") + "。"


def clean_utterance_transcript(text: object) -> str:
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", str(text or "").strip())
    for homophone in ("素材", "素菜"):
        value = value.replace(f"{homophone}状态", "数采状态").replace(f"{homophone}系统", "数采系统")
    return re.sub(r"\s+", " ", value).strip()


def strip_wake_prefix(text: object, wake_phrases: Iterable[str]) -> str:
    value = str(text or "").strip()
    for phrase in sorted(wake_phrases, key=len, reverse=True):
        value = re.sub(rf"^\s*{re.escape(phrase)}(?:\s|[，。！？,.!?:：；;-])*", "", value, count=1, flags=re.IGNORECASE)
    return value.strip()
