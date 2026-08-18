import unittest
from types import SimpleNamespace

from insight_capture.api.websocket import PoseWebSocketService


class _Node:
    pose_publish_hz = 30.0

    def __init__(self):
        self.disconnected = 0

    def viewer_disconnected(self):
        self.disconnected += 1


class WebSocketLeaseTest(unittest.TestCase):
    def test_discard_releases_viewer_exactly_once(self):
        node = _Node()
        service = PoseWebSocketService(SimpleNamespace(node=node))
        client = object()
        service.clients.add(client)

        service._discard_client(client)
        service._discard_client(client)

        self.assertEqual(node.disconnected, 1)
        self.assertNotIn(client, service.clients)


if __name__ == "__main__":
    unittest.main()
