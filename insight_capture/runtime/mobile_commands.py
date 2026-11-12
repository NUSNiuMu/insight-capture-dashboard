"""Durable receipts for phone commands whose HTTP response may be lost."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class MobileCommandStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY, action TEXT NOT NULL,
                output_path TEXT, state TEXT NOT NULL, operation TEXT NOT NULL
            )""")
            # The writer may have acted before the process stopped. Do not
            # describe an unfinished receipt as success, or replay its action.
            for command_id, raw in db.execute(
                "SELECT command_id, operation FROM commands WHERE state = 'running'"
            ).fetchall():
                operation = json.loads(raw)
                operation.update(state="interrupted", error=(
                    "设备服务已重启，上次操作结果无法确认。请查看当前状态和录制记录。"
                ))
                db.execute("UPDATE commands SET state = ?, operation = ? WHERE command_id = ?",
                           (operation["state"], json.dumps(operation), command_id))

    def get(self, command_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute(
                "SELECT action, output_path, operation FROM commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        return ((row[0], row[1]), json.loads(row[2])) if row else None

    def reserve(self, fingerprint, operation):
        # Commit the receipt before starting the side effect. Keep old IDs so a
        # delayed request cannot create a second Take after cache eviction.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO commands VALUES (?, ?, ?, ?, ?)",
                       (operation["command_id"], *fingerprint,
                        operation["state"], json.dumps(operation)))

    def finish(self, operation):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE commands SET state = ?, operation = ? WHERE command_id = ?",
                       (operation["state"], json.dumps(operation), operation["command_id"]))
