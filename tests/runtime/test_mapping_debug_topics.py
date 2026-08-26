import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from insight_capture.runtime.mapping.insight3_localizer import (  # noqa: E402
    CAMERAS,
    Insight3GlobalLocalizer,
)
from insight_capture.runtime.mapping.insight9_mapper import (  # noqa: E402
    Insight9SparseMapper,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.messages = []

    def info(self, message: str) -> None:
        self.messages.append(message)


class _FakeTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeNode:
    def __init__(self) -> None:
        self._args = SimpleNamespace(path_publish_hz=2.0)
        self._debug_topics_lock = threading.Lock()
        self._map_lock = threading.Lock()
        self._pointcloud_publisher = None
        self._path_publisher = None
        self._path_publishers = {}
        self._path_timer = None
        self._pointcloud_dirty = False
        self._last_pointcloud_publish_monotonic = 1.0
        self.created_publishers = []
        self.destroyed_publishers = []
        self.created_timers = []
        self.destroyed_timers = []
        self.logger = _FakeLogger()

    def create_publisher(self, _message_type, topic: str, _qos):
        publisher = SimpleNamespace(topic=topic)
        self.created_publishers.append(publisher)
        return publisher

    def destroy_publisher(self, publisher) -> None:
        self.destroyed_publishers.append(publisher)

    def create_timer(self, period: float, callback):
        timer = _FakeTimer()
        timer.period = period
        timer.callback = callback
        self.created_timers.append(timer)
        return timer

    def destroy_timer(self, timer) -> None:
        self.destroyed_timers.append(timer)

    def get_logger(self) -> _FakeLogger:
        return self.logger

    def _publish_path(self) -> None:
        pass

    def _publish_paths(self) -> None:
        pass


class MappingDebugTopicsTest(unittest.TestCase):
    def test_mapper_creates_and_removes_debug_resources(self):
        node = _FakeNode()

        changed = Insight9SparseMapper._set_debug_topics_enabled(node, True)

        self.assertTrue(changed)
        self.assertEqual(
            [publisher.topic for publisher in node.created_publishers],
            ["insight9_sparse_map/points", "insight9_sparse_map/path"],
        )
        self.assertEqual(len(node.created_timers), 1)
        self.assertAlmostEqual(node.created_timers[0].period, 0.5)
        self.assertTrue(node._pointcloud_dirty)
        self.assertEqual(node._last_pointcloud_publish_monotonic, float("-inf"))
        self.assertFalse(
            Insight9SparseMapper._set_debug_topics_enabled(node, True)
        )

        changed = Insight9SparseMapper._set_debug_topics_enabled(node, False)

        self.assertTrue(changed)
        self.assertTrue(node.created_timers[0].cancelled)
        self.assertEqual(node.destroyed_timers, node.created_timers)
        self.assertEqual(node.destroyed_publishers, node.created_publishers)
        self.assertIsNone(node._pointcloud_publisher)
        self.assertIsNone(node._path_publisher)
        self.assertIsNone(node._path_timer)

    def test_localizer_creates_and_removes_both_path_publishers(self):
        node = _FakeNode()

        changed = Insight3GlobalLocalizer._set_debug_topics_enabled(node, True)

        self.assertTrue(changed)
        self.assertEqual(
            [publisher.topic for publisher in node.created_publishers],
            [f"insight_global/{name}/path" for name in CAMERAS],
        )
        self.assertEqual(len(node.created_timers), 1)
        self.assertFalse(
            Insight3GlobalLocalizer._set_debug_topics_enabled(node, True)
        )

        changed = Insight3GlobalLocalizer._set_debug_topics_enabled(node, False)

        self.assertTrue(changed)
        self.assertTrue(node.created_timers[0].cancelled)
        self.assertEqual(node.destroyed_timers, node.created_timers)
        self.assertEqual(node.destroyed_publishers, node.created_publishers)
        self.assertEqual(node._path_publishers, {})
        self.assertIsNone(node._path_timer)


if __name__ == "__main__":
    unittest.main()
