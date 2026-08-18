"""Optional OpenClaw request and response formatting."""

from pathlib import Path


def build_agent_command(openclaw_bin: Path, session_key: str, utterance: str, timeout_sec: int, thinking_level: str = "off", model: str = "openai/gpt-5.6-luna") -> list[str]:
    prompt = "以下内容由本地麦克风离线转写。按宸境语音助手规则处理；除非用户明确要求详情，否则最多用两句简短中文回答。\n\n" f"用户说：{utterance}"
    return [str(openclaw_bin), "agent", "--session-key", session_key, "--model", model, "--message", prompt, "--thinking", thinking_level, "--timeout", str(timeout_sec), "--json"]


def extract_openclaw_reply(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("OpenClaw returned a non-object JSON payload")
    payload = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content.strip()
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("finalAssistantVisibleText"), str) and meta["finalAssistantVisibleText"].strip():
        return meta["finalAssistantVisibleText"].strip()
    texts = [item["text"].strip() for item in payload.get("payloads", []) if isinstance(item, dict) and isinstance(item.get("text"), str)]
    reply = "\n".join(text for text in texts if text).strip()
    if not reply:
        raise ValueError("OpenClaw returned no assistant text")
    return reply
