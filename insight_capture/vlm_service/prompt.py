"""Prompts and answer parsing for VLM pre-annotation; no model dependencies.

Two requests per recording: `task` names what the whole recording performs
(from the action table when there is one), `segments` splits one stretch of
motion into timed subtasks of that task. Both answers are validated here so
a bad reply turns into flags for the annotator instead of wrong labels.
"""

import json
import re

CJK = re.compile(r"[㐀-鿿]")

CONTEXT = (
    "这是第一人称视角的机器人示教数据采集视频。画面中有两个手持夹爪（UMI 采集器），"
    "每个夹爪上固定着一个贴有黑白 ArUco 二维码的白色方块——这些方块只是夹爪自身的定位标记，"
    "不是被操作的物体。画面左侧是左夹爪，右侧是右夹爪；夹爪可能被人手拿起操作，也可能静止放在桌上。"
    "人手有时会直接摆放物体以复位场景。"
)
IMPERATIVE = "以动词开头的英文祈使句，写出画面里具体的物体、所用夹爪或方向，不要写成描述句"


def _subtasks_text(task):
    return "\n".join(
        f"  - subtask `{s['id']}`: {s['text']}" + (f"（{s['zh']}）" if s.get("zh") else "")
        for s in task.get("subtasks", [])
    )


def _task_text(task):
    line = f"- task `{task['id']}`: {task['text']}" + (f"（{task['zh']}）" if task.get("zh") else "")
    return line + (f" —— {task['description']}" if task.get("description") else "")


def build_task_prompt(duration_s, table=None):
    head = CONTEXT + f"下面是一条录制中所有动作部分拼起来的画面（约 {duration_s:.0f} 秒，复位现场的停顿已去掉）。"
    if table and table.get("tasks"):
        listing = "\n".join(_task_text(t) + "\n" + _subtasks_text(t) for t in table["tasks"])
        return (
            head + "\n动作表如下，task_id 只能从中选择：\n" + listing
            + "\n\n请判断这条录制在执行哪个 task，输出一个 JSON 对象，不要输出其他内容：\n"
            '{"task_id": "最符合的 task id；都不符合则为空字符串", '
            f'"task": "用你自己的英文措辞写这个任务，含义与所选 task 一致但不要照抄动作表；{IMPERATIVE}", '
            '"description": "用中文一句话概括录制里做了什么"}'
        )
    return (
        head + "请判断这条录制在执行什么任务，输出一个 JSON 对象，不要输出其他内容：\n"
        f'{{"task": "整条录制的任务，{IMPERATIVE}", "description": "用中文一句话概括录制里做了什么"}}'
    )


def build_segment_prompt(duration_s, task=None, task_text=""):
    head = CONTEXT + f"下面是录制中连续动作的一段，共 {duration_s:.1f} 秒，画面里标注了从 0 开始的时间戳。"
    if task and task.get("subtasks"):
        body = (
            f"这段在执行这个 task：\n{_task_text(task)}\n{_subtasks_text(task)}\n\n"
            "这些 subtask 通常按上面列出的顺序依次执行，并可能整组重复多次；这一段可以从组内任意一步开始。"
            "请把这一段切分成动作片段，每个片段只包含一个 subtask，subtask_id 只能从上面选择。"
        )
        item = (
            '{"start": 开始秒数, "end": 结束秒数, "subtask_id": "subtask id", '
            f'"subtask": "用你自己的英文措辞写这一步，{IMPERATIVE}"}}'
        )
    else:
        body = (
            (f"整条录制的任务是：{task_text}\n" if task_text else "")
            + "请把这一段切分成动作片段，每个片段只包含一个动作步骤。"
        )
        item = f'{{"start": 开始秒数, "end": 结束秒数, "subtask": "这一步，{IMPERATIVE}"}}'
    return (
        head + body
        + "片段从动作开始的时刻到动作完成的时刻，时间精确到 0.5 秒；动作之间的停顿不算在片段内，"
        "没有动作的时间不要输出片段。输出一个 JSON 对象，不要输出其他内容：\n"
        f'{{"segments": [{item}, ...]}}'
    )


def _json_object(answer):
    match = re.search(r"\{.*\}", answer, re.S)
    try:
        value = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _english(value, key, flags):
    """Training text is English; Chinese (or a placeholder such as「未知任务」)
    means the model ignored the prompt, so it is dropped with a flag."""
    value = value.strip() if isinstance(value, str) else ""
    if CJK.search(value):
        flags.append(f"模型把 {key} 写成了中文，已丢弃")
        return ""
    return value


def _by_text(entries, text):
    key = text.strip().rstrip(".").lower()
    return next((x for x in entries if key and x["text"].strip().rstrip(".").lower() == key), None)


def parse_task_answer(answer, table=None):
    parsed = _json_object(answer)
    if parsed is None:
        return {"task_id": "", "task": "", "description": "", "flags": ["模型输出无法解析"]}
    flags = []
    label = {
        "task_id": str(parsed.get("task_id") or "").strip(),
        "task": _english(parsed.get("task"), "task", flags),
        "description": str(parsed.get("description") or "").strip(),
    }
    tasks = {t["id"]: t for t in (table or {}).get("tasks", [])}
    if tasks:
        task = tasks.get(label["task_id"])
        if task is None and not label["task_id"]:
            # The model often copies a canonical sentence but leaves the id out.
            task = _by_text(tasks.values(), label["task"])
        if task is None:
            flags.append(
                f"模型给出的 task_id {label['task_id']} 不在动作表中"
                if label["task_id"] else "模型未能匹配动作表中的 task"
            )
            label["task_id"] = ""
        else:
            label["task_id"] = task["id"]
            label["task"] = label["task"] or task["text"]
    else:
        label["task_id"] = ""
    if not label["task"]:
        flags.append("模型没有给出任务文本")
    label["flags"] = flags
    return label


def parse_segments_answer(answer, duration_s, task=None):
    """Timed subtasks inside [0, duration_s], sorted and not overlapping."""
    parsed = _json_object(answer)
    if parsed is None or not isinstance(parsed.get("segments"), list):
        return {"segments": [], "flags": ["模型输出无法解析"]}
    subtasks = {s["id"]: s for s in (task or {}).get("subtasks", [])}
    segments = []
    for item in parsed["segments"]:
        if not isinstance(item, dict):
            continue
        try:
            start = min(max(float(item.get("start")), 0.0), duration_s)
            end = min(max(float(item.get("end")), 0.0), duration_s)
        except (TypeError, ValueError):
            continue
        if end - start < 0.2:
            continue
        flags = []
        text = _english(item.get("subtask"), "subtask", flags)
        sub_id = str(item.get("subtask_id") or "").strip()
        sub = subtasks.get(sub_id)
        if subtasks:
            if sub is None and sub_id:
                flags.append(f"subtask_id {sub_id} 不属于所选 task")
            elif sub is None:
                sub = _by_text(subtasks.values(), text)
                if sub is None:
                    flags.append("模型未能匹配动作表中的 subtask")
        text = text or (sub["text"] if sub else "")
        if not text:
            flags.append("模型没有给出子任务文本")
        segments.append(
            {"start": round(start, 2), "end": round(end, 2), "subtask_id": sub["id"] if sub else "",
             "subtask": text, "flags": flags}
        )
    segments.sort(key=lambda s: s["start"])
    for previous, current in zip(segments, segments[1:]):
        if current["start"] < previous["end"]:  # the model sometimes overlaps neighbours by a second
            middle = round((current["start"] + previous["end"]) / 2, 2)
            previous["end"], current["start"] = middle, middle
    return {"segments": [s for s in segments if s["end"] - s["start"] >= 0.2], "flags": []}
