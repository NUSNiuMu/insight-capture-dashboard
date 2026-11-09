import unittest

import numpy as np

import tempfile
from pathlib import Path

from insight_capture.aruco_capture.preannotate import (
    load_url,
    normalize_url,
    probe,
    sample_times,
    save_url,
    segment_hands,
)
from insight_capture.vlm_service.prompt import build_prompt, parse_answer

TABLE = {
    "version": "v1",
    "tasks": [
        {
            "id": "move_cup_right",
            "text": "Move the cup to the right",
            "zh": "把杯子移到右边",
            "subtasks": [
                {"id": "grasp_cup", "text": "Grasp the cup", "zh": ""},
                {"id": "carry_cup", "text": "Carry the cup to the right", "zh": ""},
            ],
        }
    ],
}


class ParseAnswerTests(unittest.TestCase):
    def test_valid_ids_and_free_text(self):
        label = parse_answer(
            'ok {"task_id": "move_cup_right", "subtask_id": "grasp_cup", '
            '"task": "Slide the paper cup rightwards", "subtask": "", "description": "左手抓杯"}',
            TABLE,
        )
        self.assertEqual(label["task_id"], "move_cup_right")
        self.assertEqual(label["task"], "Slide the paper cup rightwards")
        self.assertEqual(label["subtask"], "Grasp the cup")  # canonical fallback
        self.assertEqual(label["flags"], [])

    def test_ids_recovered_from_canonical_text(self):
        label = parse_answer(
            '{"task_id": "", "subtask_id": "", "task": "move the cup to the right.", "subtask": "Carry the cup to the right"}',
            TABLE,
        )
        self.assertEqual((label["task_id"], label["subtask_id"]), ("move_cup_right", "carry_cup"))
        self.assertEqual(label["flags"], [])

    def test_unknown_ids_and_chinese_are_flagged(self):
        label = parse_answer('{"task_id": "fold", "task": "未知任务", "subtask": "Grasp"}', TABLE)
        self.assertEqual((label["task_id"], label["task"]), ("", ""))
        self.assertEqual(len(label["flags"]), 3)
        label = parse_answer('{"task_id": "move_cup_right", "subtask_id": "fold"}', TABLE)
        self.assertEqual((label["subtask_id"], label["task"]), ("", "Move the cup to the right"))
        self.assertEqual(len(label["flags"]), 1)

    def test_unparsable_and_no_table(self):
        self.assertEqual(parse_answer("I cannot tell", TABLE)["flags"], ["模型输出无法解析"])
        label = parse_answer('{"task_id": "x", "task": "Stack the cups"}', None)
        self.assertEqual((label["task_id"], label["task"], label["flags"]), ("", "Stack the cups", []))

    def test_prompt_lists_table_and_hands(self):
        prompt = build_prompt(3.0, "left", TABLE)
        self.assertIn("`move_cup_right`", prompt)
        self.assertIn("`carry_cup`", prompt)
        self.assertIn("左夹爪", prompt)
        self.assertNotIn("task_id", build_prompt(3.0))


class HandsTests(unittest.TestCase):
    def test_hands_from_moving_share(self):
        n = 100
        idle, busy, some = np.zeros(n, bool), np.ones(n, bool), np.arange(n) % 2 == 0
        self.assertEqual(segment_hands({"left": busy, "right": idle}, 0, n), "left")
        self.assertEqual(segment_hands({"left": idle, "right": some}, 0, n), "right")
        self.assertEqual(segment_hands({"left": busy, "right": some}, 0, n), "both")
        self.assertEqual(segment_hands({"left": idle, "right": idle}, 0, n), "both")

    def test_sample_times_even_and_inside(self):
        times = sample_times(2.0, 5.0, 7)
        self.assertEqual(len(times), 6)
        self.assertTrue(((times > 2.0) & (times < 5.0)).all())

    def test_probe_without_service(self):
        self.assertIsNone(probe(""))
        self.assertIsNone(probe("http://127.0.0.1:9", timeout=0.5))


class ServiceUrlTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_url(" 10.198.1.220:8780/ "), "http://10.198.1.220:8780")
        self.assertEqual(normalize_url("https://gpu.local"), "https://gpu.local")
        self.assertEqual(normalize_url(""), "")
        for bad in ("ftp://x:1", "http://:8780", "http://x:99999", "http://x:80?a=1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_url(bad)

    def test_saved_url_overrides_default(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            self.assertEqual(load_url(root, "127.0.0.1:8780"), "http://127.0.0.1:8780")
            save_url(root, "10.0.0.5:9000")
            self.assertEqual(load_url(root, "127.0.0.1:8780"), "http://10.0.0.5:9000")
            save_url(root, "")  # cleared on the page: disabled, not back to the default
            self.assertEqual(load_url(root, "127.0.0.1:8780"), "")


if __name__ == "__main__":
    unittest.main()
