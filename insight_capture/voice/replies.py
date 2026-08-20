"""Offline operator replies and speech text cleanup."""

import re
from typing import Iterable


CANNED_REPLIES = {
    "recording_starting": "初始化录制中，请稍等。",
    "recording_started": "录制已经开始。",
    "recording_stopping": "正在结束录制。",
    "recording_stopped": "录制已经结束。请将左右手相机放回检测位，并用头部相机扫视已建图工作区，然后说检查相机。",
    "calibration_started": "校准已经开始。",
    "calibration_completed": "校准完成。",
    "capture_check_started": "开始检测。",
    "capture_reference_saved": "双手检测位和头部相机地图基准已经记录。",
    "capture_check_pass": "双手相机位置和头部相机地图闭环正常，可以开始下一次采集。",
    "capture_check_retry": "检测尚未通过。请确认左右手相机完全归位，并用头部相机扫视已建图工作区，然后再说检查相机。",
    "capture_check_recalibrate": "相机位置或头部地图闭环异常。请说开始校准，完成后重新设置检测位。",
    "capture_check_not_ready": "检测条件未满足。请确认左右手相机已经静止，头部相机和两路全局定位服务在线。",
    "capture_check_no_reference": "还没有检测位基准。请放好左右手相机，确认建图完成，然后说设置检测位。",
    "recording_already_active": "当前已经在录制。",
    "command_failed": "指令执行失败，请检查数采服务。",
}

MISHEARD_REPLY = "没听清。"
OPENCLAW_UNAVAILABLE_REPLY = "OpenClaw 暂时不可用。"


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
