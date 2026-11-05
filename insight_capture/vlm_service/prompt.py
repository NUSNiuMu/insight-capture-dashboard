"""Prompt and answer parsing for segment labelling; no model dependencies."""

import json
import re

CJK = re.compile(r"[\u3400-\u9fff]")

CONTEXT = (
    "这是第一人称视角的机器人示教数据采集视频。画面中有两个手持夹爪（UMI 采集器），"
    "每个夹爪上固定着一个贴有黑白 ArUco 二维码的白色方块——这些方块只是夹爪自身的定位标记，"
    "不是被操作的物体。画面左侧是左夹爪，右侧是右夹爪；夹爪可能被人手拿起操作，也可能静止放在桌上。"
    "人手有时会直接摆放物体以复位场景。"
)
HANDS_TEXT = {
    "left": "位姿数据显示本片段主要是左夹爪在动",
    "right": "位姿数据显示本片段主要是右夹爪在动",
    "both": "位姿数据显示本片段两个夹爪都在动",
}


def _table_text(table):
    lines = []
    for task in table["tasks"]:
        name = task["text"] + (f"（{task['zh']}）" if task.get("zh") else "")
        note = f" —— {task['description']}" if task.get("description") else ""
        lines.append(f"- task `{task['id']}`: {name}{note}")
        for sub in task.get("subtasks", []):
            sub_name = sub["text"] + (f"（{sub['zh']}）" if sub.get("zh") else "")
            lines.append(f"  - subtask `{sub['id']}`: {sub_name}")
    return "\n".join(lines)


def build_prompt(duration_s, hands=None, table=None):
    head = CONTEXT + f"下面是其中一个 {duration_s:.1f} 秒的片段"
    head += f"（{HANDS_TEXT[hands]}）。" if hands in HANDS_TEXT else "。"
    if table and table.get("tasks"):
        return (
            head
            + "\n动作表如下，task_id 和 subtask_id 只能从中选择：\n"
            + _table_text(table)
            + "\n\n请只根据这个片段的画面，输出一个 JSON 对象，不要输出其他内容：\n"
            '{"task_id": "最符合本片段的 task id；都不符合则为空字符串", '
            '"subtask_id": "所选 task 下最符合本片段的 subtask id；静止、无有效操作或无法判断则为空字符串", '
            '"task": "用你自己的英文措辞写这个任务，含义与所选 task 一致，但写出画面里具体的物体、颜色、所用夹爪或方向，不要照抄动作表的说法；必须是英文", '
            '"subtask": "同样用自己的英文措辞写本片段正在执行的步骤，必须是英文；静止或无有效操作则为空字符串", '
            '"description": "用中文一句话描述本片段发生了什么"}'
        )
    return (
        head
        + "请只根据这个片段的画面，输出一个 JSON 对象，不要输出其他内容：\n"
        '{"task": "本片段所属的总体任务，英文祈使句，简短，写出具体物体，如 Move the paper cup to the right", '
        '"subtask": "本片段内正在执行的具体步骤，英文祈使句，简短；若本片段只是静止或无有效操作则为空字符串", '
        '"description": "用中文一句话描述本片段发生了什么"}'
    )


def parse_answer(answer, table=None):
    """Turn the model's reply into a label; `flags` lists what a human should check."""
    flags = []
    match = re.search(r"\{.*\}", answer, re.S)
    try:
        parsed = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        return {
            "task_id": "", "subtask_id": "", "task": "", "subtask": "",
            "description": "", "flags": ["模型输出无法解析"],
        }

    def text(key):
        value = parsed.get(key, "")
        return value.strip() if isinstance(value, str) else ""

    label = {key: text(key) for key in ("task_id", "subtask_id", "task", "subtask", "description")}
    # Training text is English; Chinese here is the model ignoring the
    # prompt or a placeholder such as「未知任务」, so it is not kept.
    for key in ("task", "subtask"):
        if CJK.search(label[key]):
            flags.append(f"模型把 {key} 写成了中文，已丢弃")
            label[key] = ""
    tasks = {t["id"]: t for t in (table or {}).get("tasks", [])}
    if tasks:
        task = tasks.get(label["task_id"])
        if task is None and not label["task_id"]:
            # The model often copies a canonical sentence but leaves the id out.
            task = _by_text(tasks.values(), label["task"])
        if task is None:
            flags.append(
                f"模型给出的 task_id {label['task_id']} 不在动作表中"
                if label["task_id"]
                else "模型未能匹配动作表中的 task"
            )
            label["task_id"] = label["subtask_id"] = ""
        else:
            label["task_id"] = task["id"]
            subtasks = {x["id"]: x for x in task.get("subtasks", [])}
            sub = subtasks.get(label["subtask_id"])
            if sub is None and label["subtask_id"]:
                flags.append(f"subtask_id {label['subtask_id']} 不属于 task {task['id']}")
            elif sub is None:
                sub = _by_text(subtasks.values(), label["subtask"])
            label["subtask_id"] = sub["id"] if sub else ""
            # Fall back to the canonical wording when the model wrote none.
            label["task"] = label["task"] or task["text"]
            if sub:
                label["subtask"] = label["subtask"] or sub["text"]
    else:
        label["task_id"] = label["subtask_id"] = ""
    if not label["task"]:
        flags.append("模型没有给出任务文本")
    label["flags"] = flags
    return label


def _by_text(entries, text):
    key = text.strip().rstrip(".").lower()
    return next((x for x in entries if key and x["text"].strip().rstrip(".").lower() == key), None)
