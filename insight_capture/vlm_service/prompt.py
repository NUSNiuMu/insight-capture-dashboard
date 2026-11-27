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


def build_segment_prompt(duration_s, task=None, task_text="", views=()):
    """`views`: labels of the synchronised videos sent before the prompt
    (head camera first, then wrist cameras); empty for one head-camera video."""
    if views:
        head = (
            CONTEXT + f"上面依次给出同一段连续动作的 {len(views)} 路视频（{'、'.join(views)}），"
            f"它们时间同步、都从 0 开始，共 {duration_s:.1f} 秒。头部相机看全局和物体位置；"
            "腕部相机是装在夹爪上的鱼眼相机，看得清夹爪是否夹住、何时松开物体，"
            "判断抓取与放下的时刻以腕部相机为准。"
        )
    else:
        head = CONTEXT + f"下面是录制中连续动作的一段，共 {duration_s:.1f} 秒，画面里标注了从 0 开始的时间戳。"
    if task and task.get("subtasks"):
        body = (
            f"这段在执行这个 task：\n{_task_text(task)}\n{_subtasks_text(task)}\n\n"
            "这些 subtask 通常按上面列出的顺序依次执行，并可能整组重复多次；这一段可以从组内任意一步开始。"
            "请把这一段切分成动作片段，每个片段只包含一个 subtask，subtask_id 只能从上面选择。"
        )
        item = (
            '{"start": 开始秒数, "end": 结束秒数, "subtask_id": "subtask id", '
            f'"subtask": "用你自己的英文措辞写这一步，{IMPERATIVE}", '
            '"confidence": 0 到 1 之间的小数}'
        )
    else:
        body = (
            (f"整条录制的任务是：{task_text}\n" if task_text else "")
            + "请把这一段切分成动作片段，每个片段只包含一个动作步骤。"
        )
        item = (
            f'{{"start": 开始秒数, "end": 结束秒数, "subtask": "这一步，{IMPERATIVE}", '
            '"confidence": 0 到 1 之间的小数}'
        )
    return (
        head + body
        + "片段从动作开始的时刻到动作完成的时刻，时间精确到 0.5 秒；动作之间的停顿不算在片段内，"
        "没有动作的时间不要输出片段。confidence 是你对这个片段的 subtask 和起止时间判断正确的把握："
        "画面清楚、动作明确接近 1，被遮挡、动作含糊或拿不准边界时给低分。"
        "输出一个 JSON 对象，不要输出其他内容：\n"
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
        try:
            confidence = round(min(max(float(item.get("confidence")), 0.0), 1.0), 2)
        except (TypeError, ValueError):
            confidence = None   # older prompt or the model left it out
        segments.append(
            {"start": round(start, 2), "end": round(end, 2), "subtask_id": sub["id"] if sub else "",
             "subtask": text, "flags": flags, "confidence": confidence}
        )
    segments.sort(key=lambda s: s["start"])
    for previous, current in zip(segments, segments[1:]):
        if current["start"] < previous["end"]:  # the model sometimes overlaps neighbours by a second
            middle = round((current["start"] + previous["end"]) / 2, 2)
            previous["end"], current["start"] = middle, middle
    return {"segments": [s for s in segments if s["end"] - s["start"] >= 0.2], "flags": []}


def build_label_prompt(spans, task=None, task_text="", previous=""):
    """Segments a person cut (``spans``: [start, end] seconds in the clip, in
    order): name each one's single subtask. Seen together so the model can
    follow the sequence (which cup, which repetition); the times are fixed."""
    listing = "；".join(f"片段 {i + 1}：{a:.1f}–{b:.1f} 秒" for i, (a, b) in enumerate(spans))
    head = (
        CONTEXT + "下面的视频画面里标注了从 0 开始的时间戳。其中有人工切好的 "
        f"{len(spans)} 个动作片段，按时间先后为：{listing}。"
        "每个片段只包含一个动作步骤（subtask），起止时间已经确定，不要改动或再切分；片段之间的画面只作参考。"
    )
    context = f"紧挨在这些片段之前的一段已标注为：{previous}。" if previous else ""
    if task and task.get("subtasks"):
        body = (
            f"录制在执行这个 task：\n{_task_text(task)}\n{_subtasks_text(task)}\n" + context
            + "这些 subtask 通常按上面列出的顺序依次执行，并可能整组重复多次；请结合先后顺序判断每个片段是哪个 subtask，"
            "subtask_id 只能从上面选择，都不符合时为空字符串。"
        )
        item = (
            '{"index": 片段序号, "subtask_id": "subtask id", '
            f'"subtask": "用你自己的英文措辞写这一步，{IMPERATIVE}", "confidence": 0 到 1 之间的小数}}'
        )
    else:
        body = (f"整条录制的任务是：{task_text}\n" if task_text else "") + context + "请写出每个片段在做的动作步骤。"
        item = f'{{"index": 片段序号, "subtask": "这一步，{IMPERATIVE}", "confidence": 0 到 1 之间的小数}}'
    return (
        head + body + f"每个片段输出一条，共 {len(spans)} 条。输出一个 JSON 对象，不要输出其他内容：\n"
        f'{{"segments": [{item}, ...]}}'
    )


def _label_item(raw, subtasks):
    flags = []
    text = _english(raw.get("subtask"), "subtask", flags)
    sub_id = str(raw.get("subtask_id") or "").strip()
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
    try:
        confidence = round(min(max(float(raw.get("confidence")), 0.0), 1.0), 2)
    except (TypeError, ValueError):
        confidence = None
    return {"subtask_id": sub["id"] if sub else "", "subtask": text, "confidence": confidence, "flags": flags}


def parse_label_answer(answer, count, task=None):
    """One label per segment, in order; a segment the model skipped gets a flag."""
    parsed = _json_object(answer)
    rows = parsed.get("segments") if parsed else None
    missing = {"subtask_id": "", "subtask": "", "confidence": None}
    if not isinstance(rows, list):
        return [{**missing, "flags": ["模型输出无法解析"]} for _ in range(count)]
    subtasks = {s["id"]: s for s in (task or {}).get("subtasks", [])}
    by_index = {}
    for k, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index", k + 1)) - 1
        except (TypeError, ValueError):
            index = k
        if 0 <= index < count and index not in by_index:
            by_index[index] = row
    return [
        _label_item(by_index[i], subtasks) if i in by_index else {**missing, "flags": ["模型没有标注这一段"]}
        for i in range(count)
    ]


def build_translate_prompt(items, table=None):
    """Chinese labels typed by a person -> English training text."""
    lines = "\n".join(
        f"{i + 1}. task：{item.get('task') or '（空）'}；subtask：{item.get('subtask') or '（空）'}"
        for i, item in enumerate(items)
    )
    listing = ""
    if table and table.get("tasks"):
        listing = (
            "\n动作表如下。如果某条标注的含义与动作表中的 task / subtask 一致，同时给出对应 id"
            "（subtask_id 必须属于所给的 task_id）；不一致就留空字符串：\n"
            + "\n".join(_task_text(t) + "\n" + _subtasks_text(t) for t in table["tasks"])
        )
    return (
        "下面是机器人示教数据的人工标注，可能是中文或中英混写。请把每条的 task 和 subtask 改写成英文，"
        f"保持原意，不增删信息；写成{IMPERATIVE}。已经是英文的保持不变，空的保持为空字符串。\n"
        + lines + listing
        + f"\n\n按原顺序输出 {len(items)} 条，输出一个 JSON 对象，不要输出其他内容：\n"
        '{"items": [{"task": "英文 task", "subtask": "英文 subtask", "task_id": "", "subtask_id": ""}, ...]}'
    )


def parse_translate_answer(answer, items, table=None):
    parsed = _json_object(answer)
    rows = parsed.get("items") if parsed else None
    if not isinstance(rows, list) or len(rows) != len(items):
        return {"items": [], "flags": ["模型输出无法解析或条数不符"]}
    tasks = {t["id"]: t for t in (table or {}).get("tasks", [])}
    out = []
    for item, row in zip(items, rows):
        row = row if isinstance(row, dict) else {}
        flags = []
        result = {}
        for key in ("task", "subtask"):
            text = _english(row.get(key), key, flags)
            # An empty source stays empty; a source the model dropped keeps its text.
            result[key] = text if (item.get(key) or "").strip() else ""
            if (item.get(key) or "").strip() and not text:
                flags.append(f"{key} 未能转成英文，保留原文")
                result[key] = item.get(key)
        task = tasks.get(str(row.get("task_id") or "").strip())
        sub_id = str(row.get("subtask_id") or "").strip()
        result["task_id"] = task["id"] if task else ""
        result["subtask_id"] = sub_id if task and any(s["id"] == sub_id for s in task.get("subtasks", [])) else ""
        result["flags"] = flags
        out.append(result)
    return {"items": out, "flags": []}
