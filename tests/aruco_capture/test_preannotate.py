import unittest

import numpy as np

import tempfile
from pathlib import Path

from insight_capture.aruco_capture.preannotate import (
    load_url,
    motion_edges,
    normalize_url,
    pose_check,
    probe,
    sample_times,
    save_url,
    segment_hands,
    snap,
    windows,
)
from insight_capture.vlm_service.prompt import (
    build_segment_prompt,
    build_task_prompt,
    parse_segments_answer,
    parse_task_answer,
)

TABLE = {
    "version": "v1",
    "tasks": [
        {
            "id": "a01_move_gripper_left",
            "text": "Close and open the left gripper, then move it around",
            "zh": "左手开合并移动",
            "description": "每个运动间停顿",
            "subtasks": [
                {"id": "close_gripper", "text": "Close the left gripper", "zh": "闭合"},
                {"id": "move_left", "text": "Move the left gripper to the left", "zh": "向左"},
            ],
        },
        {"id": "a16_flatten_towel", "text": "Spread the towel flat on the table", "zh": "平铺毛巾", "subtasks": []},
    ],
}
TASK = TABLE["tasks"][0]


class TaskAnswerTests(unittest.TestCase):
    def test_valid_id_keeps_free_text(self):
        label = parse_task_answer(
            '{"task_id": "a01_move_gripper_left", "task": "Work the left gripper", "description": "左手开合"}', TABLE
        )
        self.assertEqual((label["task_id"], label["task"], label["flags"]), ("a01_move_gripper_left", "Work the left gripper", []))

    def test_id_recovered_from_canonical_text_and_fallback(self):
        label = parse_task_answer('{"task": "spread the towel flat on the table."}', TABLE)
        self.assertEqual(label["task_id"], "a16_flatten_towel")
        label = parse_task_answer('{"task_id": "a16_flatten_towel", "task": "铺毛巾"}', TABLE)
        self.assertEqual(label["task"], "Spread the towel flat on the table")  # Chinese dropped, canonical used
        self.assertEqual(len(label["flags"]), 1)

    def test_unknown_and_unparsable(self):
        self.assertEqual(parse_task_answer('{"task_id": "fold", "task": "Fold"}', TABLE)["task_id"], "")
        self.assertEqual(parse_task_answer("no idea", TABLE)["flags"], ["模型输出无法解析"])
        self.assertEqual(parse_task_answer('{"task_id": "x", "task": "Stack the cups"}')["task_id"], "")

    def test_prompts(self):
        self.assertIn("`a16_flatten_towel`", build_task_prompt(30, TABLE))
        prompt = build_segment_prompt(12.0, TASK)
        self.assertIn("`close_gripper`", prompt)
        self.assertIn("每个运动间停顿", prompt)
        self.assertNotIn("subtask_id", build_segment_prompt(12.0, None, "Fold the towel"))


class SegmentsAnswerTests(unittest.TestCase):
    def test_clamped_sorted_and_without_overlap(self):
        answer = (
            '```json {"segments": ['
            '{"start": 4, "end": 7, "subtask_id": "move_left", "subtask": "Swing the gripper left"},'
            '{"start": -1, "end": 5, "subtask_id": "close_gripper", "subtask": ""},'
            '{"start": 9, "end": 30, "subtask_id": "fold", "subtask": "Fold it"},'
            '{"start": 8, "end": 8.1, "subtask_id": "move_left"}]} ```'
        )
        result = parse_segments_answer(answer, 12.0, TASK)["segments"]
        self.assertEqual([(s["start"], s["end"], s["subtask_id"]) for s in result],
                         [(0.0, 4.5, "close_gripper"), (4.5, 7.0, "move_left"), (9.0, 12.0, "")])
        self.assertEqual(result[0]["subtask"], "Close the left gripper")  # canonical fallback
        self.assertEqual(len(result[2]["flags"]), 1)  # id outside the task

    def test_unparsable(self):
        self.assertEqual(parse_segments_answer("sorry", 5.0, TASK), {"segments": [], "flags": ["模型输出无法解析"]})


class PlanningTests(unittest.TestCase):
    def test_sample_times_even_and_inside(self):
        times = sample_times(2.0, 5.0, 2.5)
        self.assertEqual(len(times), 6)
        self.assertTrue(((times > 2.0) & (times < 5.0)).all())

    def test_windows_overlap_for_long_stretches(self):
        self.assertEqual(windows(0, 30), [(0, 30)])
        self.assertEqual(windows(0, 100), [(0, 40), (35, 75), (70, 100)])

    def test_snap_to_nearby_motion_edge(self):
        grid = np.arange(100) / 10
        active = np.zeros(99, dtype=bool)
        active[20:40] = True  # moving from 2.0 s to 4.0 s
        onsets, offsets = motion_edges(grid, active)
        self.assertEqual((list(onsets), list(offsets)), ([2.0], [4.0]))
        self.assertEqual(snap(2.5, onsets, 0, 10), 2.0)
        self.assertEqual(snap(3.0, onsets, 0, 10), 3.0)  # nothing within 0.75 s
        self.assertEqual(snap(2.5, onsets, 2.2, 10), 2.5)  # edge outside the stretch


class PoseCheckTests(unittest.TestCase):
    def setUp(self):
        self.state = np.zeros((20, 20), dtype=np.float32)
        self.state[:, 9] = self.state[:, 19] = 0.09
        self.good = np.ones(20, dtype=bool)

    def test_direction(self):
        self.state[:, 0] = np.linspace(0, -0.1, 20)  # left arm moves 10 cm to -x (left)
        self.assertEqual(pose_check("move_left", self.state, self.good, 0, 19, "left", True), [])
        self.assertIn("向左", pose_check("move_forward", self.state, self.good, 0, 19, "left", True)[0])
        self.assertEqual(pose_check("move_forward", self.state, self.good, 0, 19, "left", False), [])  # no gravity frame
        self.assertEqual(len(pose_check("move_up", self.state, self.good, 0, 19, "right", True)), 1)  # right arm still

    def test_single_frame_jump_is_ignored(self):
        self.state[:, 0] = np.linspace(0, -0.1, 20)
        self.state[12, 2] = 0.38  # one-frame pose jump upward
        self.assertEqual(pose_check("move_left", self.state, self.good, 0, 19, "left", True), [])
        self.good[5:15] = False  # invalid frames are not used at all
        self.state[5:15, 2] = 0.5
        self.assertEqual(pose_check("move_left", self.state, self.good, 0, 19, "left", True), [])

    def test_gripper(self):
        self.state[10:, 9] = 0.012  # left gripper closes
        self.assertEqual(pose_check("close_gripper", self.state, self.good, 0, 19, "left", True), [])
        self.assertEqual(len(pose_check("open_gripper", self.state, self.good, 0, 19, "left", True)), 1)
        self.assertEqual(pose_check("close_gripper", self.state, self.good, 0, 19, "both", True), [])


class HandsTests(unittest.TestCase):
    def test_hands_from_moving_share(self):
        n = 100
        idle, busy, some = np.zeros(n, bool), np.ones(n, bool), np.arange(n) % 2 == 0
        self.assertEqual(segment_hands({"left": busy, "right": idle}, 0, n), "left")
        self.assertEqual(segment_hands({"left": idle, "right": some}, 0, n), "right")
        self.assertEqual(segment_hands({"left": busy, "right": some}, 0, n), "both")
        self.assertEqual(segment_hands({"left": idle, "right": idle}, 0, n), "both")

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


class CoverTests(unittest.TestCase):
    def test_tiles_whole_stretch(self):
        from insight_capture.aruco_capture.preannotate import cover
        items = [{"start": 1.0, "end": 3.0}, {"start": 3.1, "end": 5.0}, {"start": 7.0, "end": 7.2}]
        out = cover(0.0, 10.0, items, 0.5)
        self.assertEqual(out[0][:2], (0.0, 1.0)); self.assertIsNone(out[0][2])        # leading gap -> other
        self.assertEqual(out[1][:2], (1.0, 3.1))                                        # tiny gap absorbed
        self.assertEqual(out[2][:2], (3.1, 5.0))
        self.assertEqual(out[-1][:2], (5.0, 10.0)); self.assertIsNone(out[-1][2])       # short item dropped, tail other
        for (a, b, _), (c, d, _) in zip(out, out[1:]):
            self.assertEqual(b, c)

    def test_empty_and_tiny_tail(self):
        from insight_capture.aruco_capture.preannotate import cover
        self.assertEqual(cover(0, 4, [], 0.5), [(0, 4, None)])
        out = cover(0, 4, [{"start": 0.2, "end": 3.8}], 0.5)
        self.assertEqual([(a, b) for a, b, _ in out], [(0, 4)])
