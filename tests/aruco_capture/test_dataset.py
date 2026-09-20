import unittest

import numpy as np

from insight_capture.aruco_capture.dataset import suggest_pause_segments

IDENTITY_6D = np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0], dtype=np.float32)


def _build(fps, left_positions, good=None, width=0.02):
    """一个静止的右手 + 给定左手轨迹的合成 state 网格，供静止检测测试使用。"""

    frame_count = len(left_positions)
    grid = np.arange(frame_count, dtype=np.float64) / fps
    state = np.zeros((frame_count, 20), dtype=np.float32)
    state[:, 3:9] = IDENTITY_6D
    state[:, 9] = width
    state[:, 13:19] = IDENTITY_6D
    state[:, 19] = width
    for i, position in enumerate(left_positions):
        state[i, 0:3] = position
    if good is None:
        good = np.ones(frame_count, dtype=bool)
    return grid, state, np.asarray(good, dtype=bool)


def _moving_positions(count, step_m, start=0.0):
    return [[start + step_m * i, 0.0, 0.0] for i in range(count)]


def _idle_positions(count, at):
    return [list(at)] * count


class SuggestPauseSegmentsTests(unittest.TestCase):
    def test_pause_between_two_moves_produces_two_segments(self):
        fps = 20
        move = _moving_positions(40, 0.02)  # 0.4 m/s, well above the idle threshold
        idle = _idle_positions(40, move[-1])  # 2s pause, above idle_duration_s
        tail = _moving_positions(40, 0.02, start=move[-1][0])
        left = move + idle + tail
        grid, state, good = _build(fps, left)

        segments = suggest_pause_segments(grid, state, good, fps)

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start_s"], 0.0)
        self.assertEqual(segments[0]["end_s"], segments[1]["start_s"])
        self.assertAlmostEqual(segments[1]["end_s"], grid[-1] + 1.0 / fps)
        for segment in segments:
            self.assertEqual(segment["task"], "")
            self.assertEqual(segment["hands"], "both")

    def test_continuous_motion_yields_a_single_segment(self):
        fps = 20
        left = _moving_positions(120, 0.02)
        grid, state, good = _build(fps, left)

        segments = suggest_pause_segments(grid, state, good, fps)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["start_s"], 0.0)
        self.assertAlmostEqual(segments[0]["end_s"], grid[-1] + 1.0 / fps)

    def test_invalid_frames_inside_a_pause_suppress_the_cut(self):
        fps = 20
        move = _moving_positions(40, 0.02)
        idle = _idle_positions(40, move[-1])  # same 2s pause as the first test
        tail = _moving_positions(40, 0.02, start=move[-1][0])
        left = move + idle + tail
        good = np.ones(len(left), dtype=bool)
        # Break the pause roughly in half with a bad stretch well longer than
        # the 0.15s gap-tolerance, leaving two ~0.55s idle runs on either
        # side -- both short of idle_duration_s (0.8s), so neither cuts.
        good[51:69] = False
        grid, state, good = _build(fps, left, good=good)

        segments = suggest_pause_segments(grid, state, good, fps)

        self.assertEqual(len(segments), 1)

    def test_too_short_recording_returns_no_segments(self):
        grid, state, good = _build(20, [[0.0, 0.0, 0.0]])

        self.assertEqual(suggest_pause_segments(grid, state, good, 20), [])


if __name__ == "__main__":
    unittest.main()
