"""Action vocabulary (tasks with their subtasks) shared by every recording.

Segments pick a canonical id from here and keep their own free-form text, so
one action can be labelled with varied language while staying countable.
"""

import csv
import io
import json
import re
import time
from pathlib import Path

from .capture import write_json

ID = re.compile(r"[A-Za-z0-9_.-]+")
CURRENT = "_action_table.json"
HISTORY = "_action_tables"
CSV_COLUMNS = ("task_id", "task", "task_zh", "subtask_id", "subtask", "subtask_zh")


def empty():
    return {"version": "", "tasks": []}


def load(root):
    path = Path(root) / CURRENT
    return json.loads(path.read_text()) if path.is_file() else empty()


def parse(content, filename=""):
    """Read an uploaded JSON or CSV action table into the validated form."""
    text = content.lstrip("﻿")
    if filename.lower().endswith(".csv") or not text.lstrip().startswith(("{", "[")):
        table = _from_csv(text)
    else:
        try:
            table = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式错误：第 {exc.lineno} 行 {exc.msg}") from exc
        if isinstance(table, list):
            table = {"tasks": table}
    return validate(table)


def validate(table):
    if not isinstance(table, dict) or not isinstance(table.get("tasks"), list):
        raise ValueError("动作表须包含 tasks 列表")
    tasks, seen = [], set()
    for i, task in enumerate(table["tasks"], 1):
        entry = _entry(task, f"第 {i} 个 task")
        if entry["id"] in seen:
            raise ValueError(f"task id 重复：{entry['id']}")
        seen.add(entry["id"])
        raw = task.get("subtasks", [])
        if not isinstance(raw, list):
            raise ValueError(f"task {entry['id']} 的 subtasks 须为列表")
        subtasks, sub_seen = [], set()
        for j, subtask in enumerate(raw, 1):
            sub = _entry(subtask, f"task {entry['id']} 的第 {j} 个 subtask")
            if sub["id"] in sub_seen:
                raise ValueError(f"task {entry['id']} 下 subtask id 重复：{sub['id']}")
            sub_seen.add(sub["id"])
            subtasks.append(sub)
        tasks.append({**entry, "subtasks": subtasks})
    if not tasks:
        raise ValueError("动作表里没有任何 task")
    return {"version": str(table.get("version") or "").strip(), "tasks": tasks}


def _entry(item, where):
    if not isinstance(item, dict):
        raise ValueError(f"{where} 格式不对，须为对象")
    ident = str(item.get("id") or "").strip()
    text = str(item.get("text") or "").strip()
    if not ID.fullmatch(ident):
        raise ValueError(f"{where} 的 id 只能用英文字母、数字和 _ . -，当前为 {ident!r}")
    if not text:
        raise ValueError(f"{where}（{ident}）缺少标准说法 text")
    return {
        "id": ident,
        "text": text,
        "zh": str(item.get("zh") or "").strip(),
        "description": str(item.get("description") or "").strip(),
    }


def _from_csv(text):
    """One row per subtask; a row with an empty subtask_id declares only the task."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 为空")
    reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]
    missing = {"task_id", "task"} - set(reader.fieldnames)
    if missing:
        raise ValueError(
            f"CSV 缺少列 {', '.join(sorted(missing))}；表头须为 {','.join(CSV_COLUMNS)}"
        )
    tasks = {}
    for line, row in enumerate(reader, 2):
        row = {key: (value or "").strip() for key, value in row.items() if key}
        if not any(row.values()):
            continue
        task_id = row.get("task_id", "")
        task = tasks.setdefault(
            task_id,
            {"id": task_id, "text": row.get("task", ""), "zh": row.get("task_zh", ""), "subtasks": []},
        )
        if row.get("task") and task["text"] and row["task"] != task["text"]:
            raise ValueError(f"CSV 第 {line} 行：同一个 task_id {task_id} 的 task 文本前后不一致")
        task["text"] = task["text"] or row.get("task", "")
        task["zh"] = task["zh"] or row.get("task_zh", "")
        if row.get("subtask_id") or row.get("subtask"):
            task["subtasks"].append(
                {"id": row.get("subtask_id", ""), "text": row.get("subtask", ""), "zh": row.get("subtask_zh", "")}
            )
    return {"tasks": list(tasks.values())}


def save(root, table, filename=""):
    """Make `table` the current one; every import is also kept under _action_tables/."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    version = table["version"] or Path(filename).stem or stamp
    stored = {**table, "version": version, "imported_at": stamp, "source": filename}
    root = Path(root)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", version)[:60]
    (root / HISTORY).mkdir(parents=True, exist_ok=True)
    write_json(root / HISTORY / f"{stamp}_{safe}.json", stored)
    write_json(root / CURRENT, stored)
    return stored
