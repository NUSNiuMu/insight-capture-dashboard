from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from post_processing_core.prepared_playback import (  # noqa: E402
    PreparedPlaybackManager,
    _cache_key,
    _compose_review_frame,
    _nearest_indices,
    _playback_frame,
    _review_cells,
    _select_recorded_streams,
)


class PreparedPlaybackTest(unittest.TestCase):
    def test_scan_uses_record_timestamps_across_different_header_clocks(self) -> None:
        class HeaderStamp:
            def __init__(self, nanoseconds: int) -> None:
                self.sec, self.nanosec = divmod(nanoseconds, 1_000_000_000)

        class ImageMessage:
            def __init__(self, header_nanoseconds: int) -> None:
                self.header = type("Header", (), {"stamp": HeaderStamp(header_nanoseconds)})()

        class PoseMessage(ImageMessage):
            def __init__(self, header_nanoseconds: int, x: float) -> None:
                super().__init__(header_nanoseconds)
                position = type("Position", (), {"x": x, "y": 0.0, "z": 0.0})()
                orientation = type(
                    "Orientation",
                    (),
                    {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                )()
                self.pose = type(
                    "Pose", (), {"position": position, "orientation": orientation}
                )()

        class Reader:
            def __init__(self, entries: list[tuple[str, object, int]]) -> None:
                self.entries = entries
                self.index = 0

            def has_next(self) -> bool:
                return self.index < len(self.entries)

            def read_next(self) -> tuple[str, object, int]:
                entry = self.entries[self.index]
                self.index += 1
                return entry

        entries = [
            ("/epoch/image", ImageMessage(1_786_000_000_000_000_000), 10_000_000_000),
            ("/boot/image", ImageMessage(200_000_000_000), 10_001_000_000),
            ("/epoch/pose", PoseMessage(1_786_000_000_000_000_000, 0.0), 10_002_000_000),
            ("/epoch/image", ImageMessage(1_786_000_000_033_000_000), 10_033_000_000),
            ("/boot/image", ImageMessage(200_033_000_000), 10_034_000_000),
            ("/epoch/pose", PoseMessage(1_786_000_000_033_000_000, 1.0), 10_035_000_000),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = PreparedPlaybackManager(root, root / "cache")
            deserialized = []
            serialization = types.ModuleType("rclpy.serialization")
            serialization.deserialize_message = lambda raw, _message_class: deserialized.append(raw) or raw
            rclpy = types.ModuleType("rclpy")
            rclpy.serialization = serialization
            utilities = types.ModuleType("rosidl_runtime_py.utilities")
            utilities.get_message = lambda value: value
            rosidl_runtime_py = types.ModuleType("rosidl_runtime_py")
            rosidl_runtime_py.utilities = utilities
            with (
                patch.dict(
                    sys.modules,
                    {
                        "rclpy": rclpy,
                        "rclpy.serialization": serialization,
                        "rosidl_runtime_py": rosidl_runtime_py,
                        "rosidl_runtime_py.utilities": utilities,
                    },
                ),
                patch(
                    "post_processing_core.prepared_playback._open_reader",
                    return_value=(
                        Reader(entries),
                        {
                            "/epoch/image": "Image",
                            "/boot/image": "Image",
                            "/epoch/pose": "Pose",
                        },
                    ),
                ),
            ):
                result = manager._scan(
                    root,
                    [
                        {"name": "epoch", "topic": "/epoch/image"},
                        {"name": "boot", "topic": "/boot/image"},
                    ],
                    [{"name": "epoch", "topic": "/epoch/pose"}],
                )

        self.assertEqual(
            result["image_stamps"]["epoch"].tolist(),
            [10_000_000_000, 10_033_000_000],
        )
        self.assertEqual(
            result["image_stamps"]["boot"].tolist(),
            [10_001_000_000, 10_034_000_000],
        )
        self.assertEqual(
            result["poses"]["epoch"][0].tolist(),
            [10_002_000_000, 10_035_000_000],
        )
        self.assertEqual(len(deserialized), 2, "timeline scan should deserialize poses, not images")

    def test_recorded_stream_selection_allows_one_camera_and_pose(self) -> None:
        cameras = [
            {"name": "left", "topic": "/left/image"},
            {"name": "head", "topic": "/head/image"},
            {"name": "right", "topic": "/right/image"},
        ]
        poses = [
            {"name": "left", "topic": "/left/pose"},
            {"name": "head", "topic": "/head/pose"},
            {"name": "right", "topic": "/right/pose"},
        ]
        selected_cameras, selected_poses = _select_recorded_streams(
            {"/head/image", "/head/pose"}, cameras, poses
        )
        self.assertEqual([item["name"] for item in selected_cameras], ["head"])
        self.assertEqual([item["name"] for item in selected_poses], ["head"])

    def test_recorded_stream_selection_allows_image_without_pose(self) -> None:
        selected_cameras, selected_poses = _select_recorded_streams(
            {"/left/image"},
            [{"name": "left", "topic": "/left/image"}],
            [{"name": "left", "topic": "/left/pose"}],
        )
        self.assertEqual([item["name"] for item in selected_cameras], ["left"])
        self.assertEqual(selected_poses, [])

    def test_recorded_stream_selection_preserves_full_configuration_order(self) -> None:
        cameras = [
            {"name": "left", "topic": "/left/image"},
            {"name": "head", "topic": "/head/image"},
            {"name": "right", "topic": "/right/image"},
        ]
        poses = [
            {"name": "left", "topic": "/left/pose"},
            {"name": "head", "topic": "/head/pose"},
            {"name": "right", "topic": "/right/pose"},
        ]
        selected_cameras, selected_poses = _select_recorded_streams(
            {item["topic"] for item in [*cameras, *poses]}, cameras, poses
        )
        self.assertEqual(selected_cameras, cameras)
        self.assertEqual(selected_poses, poses)

    def test_recorded_stream_selection_requires_a_camera(self) -> None:
        with self.assertRaisesRegex(ValueError, "no configured camera image topic"):
            _select_recorded_streams(
                {"/left/pose"},
                [{"name": "left", "topic": "/left/image"}],
                [{"name": "left", "topic": "/left/pose"}],
            )

    def test_cache_key_includes_playback_schema(self) -> None:
        signature = [{"name": "bag.db3", "size": 10, "mtime_ns": 20}]
        configuration = {"cameras": [{"name": "camera"}], "poses": []}
        self.assertEqual(
            _cache_key(signature, configuration),
            _cache_key(signature, configuration),
        )
        self.assertNotEqual(
            _cache_key(signature, configuration),
            _cache_key(signature, {"cameras": [], "poses": []}),
        )

    def test_review_artifacts_are_stored_beside_the_rosbag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bag = root / "bag"
            review = bag / "review"
            review.mkdir(parents=True)
            video = review / "review.mp4"
            video.write_bytes(b"video")
            manager = PreparedPlaybackManager(root, root / "legacy-cache")
            self.assertEqual(manager.artifact_path("bag", "review.mp4"), video)
            with self.assertRaises(ValueError):
                manager.artifact_path("bag", "../review.mp4")

    def test_browser_stats_are_exposed_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = PreparedPlaybackManager(root, root / "legacy-cache")
            manager.record_browser_stats({"presented_fps": 29.9})
            status = manager.status()
            self.assertEqual(status["browser_stats"]["presented_fps"], 29.9)
            self.assertIn("updated_monotonic", status["browser_stats"])

    def test_playback_frame_bounds_large_images_with_even_dimensions(self) -> None:
        source = np.zeros((1920, 1088, 3), dtype=np.uint8)
        output = _playback_frame(source)
        self.assertEqual(output.shape, (720, 408, 3))

        small = np.zeros((640, 544, 3), dtype=np.uint8)
        self.assertIs(_playback_frame(small), small)

    def test_review_frame_tiles_three_cameras_into_one_surface(self) -> None:
        cells = _review_cells(3, 1200, 600)
        self.assertEqual(cells, [
            {"x": 0, "y": 0, "width": 400, "height": 600},
            {"x": 400, "y": 0, "width": 400, "height": 600},
            {"x": 800, "y": 0, "width": 400, "height": 600},
        ])
        frames = [
            np.full((80, 60, 3), (20, 30, 40), dtype=np.uint8),
            np.full((80, 60), 90, dtype=np.uint8),
            np.full((80, 60, 4), (120, 130, 140, 255), dtype=np.uint8),
        ]
        output = _compose_review_frame(
            frames,
            [{"name": f"camera_{index}"} for index in range(3)],
            [False, True, False],
            30,
            width=1200,
            height=600,
        )
        self.assertEqual(output.shape, (600, 1200, 3))
        self.assertEqual(output.dtype, np.uint8)
        self.assertGreater(float(output[:, :400].mean()), 18.0)
        self.assertGreater(float(output[:, 400:800].mean()), 18.0)
        self.assertGreater(float(output[:, 800:].mean()), 18.0)

    def test_nearest_indices_preserve_missing_frame_as_duplicate(self) -> None:
        source = np.asarray([0, 33_000_000, 100_000_000], dtype=np.int64)
        target = np.asarray([0, 33_333_333, 66_666_667, 100_000_000], dtype=np.int64)
        indices, skew_ms = _nearest_indices(source, target)
        self.assertEqual(indices.tolist(), [0, 1, 2, 2])
        self.assertAlmostEqual(float(skew_ms[2]), 33.333333, places=5)

    def test_pose_manifest_is_fixed_rate_and_bounded_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = PreparedPlaybackManager(root, root / "cache")
            count = 900
            stamps = np.arange(count, dtype=np.int64) * 10_000_000
            positions = np.column_stack(
                (np.arange(count, dtype=np.float64), np.zeros(count), np.zeros(count))
            )
            quaternions = np.tile([0.0, 0.0, 0.0, 1.0], (count, 1))
            targets = np.arange(270, dtype=np.int64) * 33_333_333
            payload = manager._pose_manifest(
                {"camera": (stamps, positions, quaternions)},
                [{"name": "camera", "role": "head"}],
                targets,
            )[0]
        self.assertEqual(len(payload["positions"]), len(targets))
        self.assertEqual(len(payload["quaternions_xyzw"]), len(targets))
        self.assertLessEqual(len(payload["trajectory"]), 600)
        self.assertTrue(all(payload["valid"]))


if __name__ == "__main__":
    unittest.main()
