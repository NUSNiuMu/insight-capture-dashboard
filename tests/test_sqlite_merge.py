from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from post_processing_core.recovery import RecordingRecovery  # noqa: E402
from post_processing_core.sqlite_merge import merge_sqlite_parts  # noqa: E402


def _create_part(
    root: Path,
    name: str,
    topics: list[tuple[str, str]],
    messages: list[tuple[str, int, bytes]],
) -> Path:
    part = root / name
    part.mkdir()
    db_path = part / f"{name}_0.db3"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema(schema_version INTEGER PRIMARY KEY,ros_distro TEXT NOT NULL);
            CREATE TABLE metadata(id INTEGER PRIMARY KEY,metadata_version INTEGER NOT NULL,metadata TEXT NOT NULL);
            CREATE TABLE topics(id INTEGER PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL,
                                serialization_format TEXT NOT NULL,offered_qos_profiles TEXT NOT NULL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,topic_id INTEGER NOT NULL,
                                  timestamp INTEGER NOT NULL,data BLOB NOT NULL);
            CREATE INDEX timestamp_idx ON messages (timestamp ASC);
            INSERT INTO schema(schema_version, ros_distro) VALUES (3, 'humble');
            """
        )
        topic_ids = {}
        for topic_id, (topic_name, topic_type) in enumerate(topics, start=1):
            topic_ids[topic_name] = topic_id
            conn.execute(
                "INSERT INTO topics VALUES (?, ?, ?, 'cdr', '')",
                (topic_id, topic_name, topic_type),
            )
        for topic_name, timestamp, data in messages:
            conn.execute(
                "INSERT INTO messages(topic_id, timestamp, data) VALUES (?, ?, ?)",
                (topic_ids[topic_name], timestamp, data),
            )
        conn.commit()
    finally:
        conn.close()
    return part


class SqliteMergeTest(unittest.TestCase):
    def test_bulk_merge_remaps_topics_and_trims_frame_startup_skew(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_a = "/insight3_a/camera/infra1/image_rect_raw"
            image_b = "/insight3_b/camera/infra1/image_rect_raw"
            static = "/tf_static"
            pose = "/insight_global/insight3_a/pose"
            part_a = _create_part(
                root,
                "part_a",
                [(image_a, "sensor_msgs/msg/Image"), (static, "tf2_msgs/msg/TFMessage")],
                [
                    (image_a, 0, b"a0"),
                    (image_a, 1_000_000_000, b"a1"),
                    (image_a, 2_000_000_000, b"a2"),
                    (static, 0, b"tf"),
                ],
            )
            part_b = _create_part(
                root,
                "part_b",
                [(image_b, "sensor_msgs/msg/Image"), (pose, "geometry_msgs/msg/PoseStamped")],
                [
                    (image_b, 1_000_000_000, b"b1"),
                    (image_b, 2_000_000_000, b"b2"),
                    (pose, 0, b"pose"),
                ],
            )
            output = root / "merged"

            result = merge_sqlite_parts([part_a, part_b], output)

            self.assertEqual(result["method"], "sqlite_bulk")
            self.assertTrue(result["trim_applied"])
            self.assertEqual(result["trim"]["trimmed_ns"], 1_000_000_000)
            db_path = output / "merged_0.db3"
            conn = sqlite3.connect(db_path)
            try:
                counts = dict(
                    conn.execute(
                        "SELECT t.name, COUNT(m.id) FROM topics t "
                        "LEFT JOIN messages m ON m.topic_id = t.id GROUP BY t.id"
                    )
                )
                first_a = conn.execute(
                    "SELECT MIN(m.timestamp) FROM messages m JOIN topics t ON t.id = m.topic_id "
                    "WHERE t.name = ?",
                    (image_a,),
                ).fetchone()[0]
                quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(quick_check, "ok")
            self.assertEqual(counts, {image_a: 2, image_b: 2, pose: 1, static: 1})
            self.assertEqual(first_a, 1_000_000_000)

            metadata = yaml.safe_load((output / "metadata.yaml").read_text())[
                "rosbag2_bagfile_information"
            ]
            self.assertEqual(metadata["message_count"], 6)
            self.assertEqual(metadata["starting_time"]["nanoseconds_since_epoch"], 1_000_000_000)
            self.assertEqual(metadata["duration"]["nanoseconds"], 1_000_000_000)
            self.assertEqual(metadata["relative_file_paths"], ["merged_0.db3"])

            for source, expected in ((part_a, 4), (part_b, 3)):
                conn = sqlite3.connect(next(source.glob("*.db3")))
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], expected)
                finally:
                    conn.close()

    def test_duplicate_topic_messages_are_combined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            topic = "/diagnostics"
            first = _create_part(
                root, "first", [(topic, "std_msgs/msg/String")], [(topic, 10, b"one")]
            )
            second = _create_part(
                root, "second", [(topic, "std_msgs/msg/String")], [(topic, 20, b"two")]
            )

            merge_sqlite_parts([first, second], root / "merged")

            conn = sqlite3.connect(root / "merged" / "merged_0.db3")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            finally:
                conn.close()

    def test_recording_merge_falls_back_to_ros2_converter(self) -> None:
        owner = type("Owner", (), {"_output_lines": [], "ros_domain_id": 10})()
        service = RecordingRecovery(owner)
        with patch(
            "post_processing_core.recovery.merge_sqlite_parts",
            side_effect=RuntimeError("unsupported input"),
        ), patch.object(service, "_convert_merge") as fallback:
            result = service.merge_recording_parts([Path("part")], Path("output"))

        fallback.assert_called_once_with([Path("part")], Path("output"))
        self.assertEqual(result["method"], "ros2_convert")
        self.assertFalse(result["trim_applied"])
        self.assertIn("falling back", owner._output_lines[0])


if __name__ == "__main__":
    unittest.main()
