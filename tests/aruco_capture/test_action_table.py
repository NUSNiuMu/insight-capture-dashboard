import json
import tempfile
import unittest
from pathlib import Path

from insight_capture.aruco_capture import action_table

TABLE = {
    "version": "cup-v1",
    "tasks": [
        {
            "id": "move_cup_right",
            "text": "Move the cup to the right",
            "zh": "把杯子移到右边",
            "subtasks": [
                {"id": "grasp_cup", "text": "Grasp the cup", "zh": "抓取杯子"},
                {"id": "release_cup", "text": "Release the cup"},
            ],
        },
        {"id": "stack_cups", "text": "Stack the cups"},
    ],
}


class ActionTableTests(unittest.TestCase):
    def test_json_round_trip(self):
        table = action_table.parse(json.dumps(TABLE), "cups.json")
        self.assertEqual(table["version"], "cup-v1")
        self.assertEqual([t["id"] for t in table["tasks"]], ["move_cup_right", "stack_cups"])
        self.assertEqual(table["tasks"][0]["subtasks"][1]["zh"], "")
        self.assertEqual(table["tasks"][1]["subtasks"], [])

    def test_csv_groups_subtasks_under_tasks(self):
        text = (
            "﻿task_id,task,task_zh,subtask_id,subtask,subtask_zh\n"
            "move_cup_right,Move the cup to the right,把杯子移到右边,grasp_cup,Grasp the cup,抓取杯子\n"
            "move_cup_right,,,release_cup,Release the cup,放下杯子\n"
            "\n"
            "stack_cups,Stack the cups,叠杯子,,,\n"
        )
        table = action_table.parse(text, "cups.csv")
        self.assertEqual(
            [(t["id"], [s["id"] for s in t["subtasks"]]) for t in table["tasks"]],
            [("move_cup_right", ["grasp_cup", "release_cup"]), ("stack_cups", [])],
        )

    def test_rejects_bad_tables(self):
        bad = [
            {"tasks": []},
            {"tasks": [{"id": "有中文", "text": "x"}]},
            {"tasks": [{"id": "a", "text": ""}]},
            {"tasks": [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]},
            {"tasks": [{"id": "a", "text": "x", "subtasks": [{"id": "s", "text": "1"}, {"id": "s", "text": "2"}]}]},
        ]
        for table in bad:
            with self.subTest(table=table), self.assertRaises(ValueError):
                action_table.parse(json.dumps(table))
        with self.assertRaises(ValueError):
            action_table.parse("{not json", "x.json")
        with self.assertRaises(ValueError):
            action_table.parse("id,text\na,b\n", "x.csv")
        with self.assertRaises(ValueError):
            action_table.parse("task_id,task\na,one\na,two\n", "x.csv")

    def test_save_keeps_history(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(action_table.load(root), action_table.empty())
            first = action_table.save(root, action_table.parse(json.dumps(TABLE)), "cups.json")
            second = action_table.save(
                root, action_table.parse(json.dumps({"tasks": [{"id": "a", "text": "x"}]})), "fold.json"
            )
            self.assertEqual(first["version"], "cup-v1")
            self.assertEqual(second["version"], "fold")
            self.assertEqual(action_table.load(root)["version"], "fold")
            self.assertEqual(len(list((Path(root) / action_table.HISTORY).glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
