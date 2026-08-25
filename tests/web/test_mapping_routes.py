import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.api.routes.mapping import MappingRoutes  # noqa: E402


class MappingRoutesTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _node(*, stale_images=(), stale_vio=(), reset_payload=None):
        now = time.monotonic()
        cameras = [
            SimpleNamespace(name="insight3_a", label="Insight3 A Infrared"),
            SimpleNamespace(name="insight3_b", label="Insight3 B Infrared"),
            SimpleNamespace(name="insight9_a", label="Insight9 A Color"),
        ]
        return SimpleNamespace(
            cameras=cameras,
            camera_stale_timeout_sec=2.0,
            camera_input_lock=threading.Lock(),
            camera_input_times={
                camera.name: ([] if camera.name in stale_images else [now])
                for camera in cameras
            },
            camera_liveness_times={
                camera.name: (0.0 if camera.name in stale_vio else now)
                for camera in cameras
            },
            reset_mapping=mock.Mock(
                return_value=reset_payload
                or {"ok": True, "requested": ["mapper", "localizer"]}
            ),
        )

    async def test_reset_checks_all_camera_images_and_vio_before_calibration(self):
        node = self._node(
            stale_images=("insight9_a",),
            stale_vio=("insight3_b", "insight9_a"),
        )
        routes = MappingRoutes(SimpleNamespace(node=node))

        response = await routes._handle_reset(None)
        body = json.loads(response.text)

        self.assertEqual(response.status, 409)
        self.assertEqual(body["error"], "calibration_cameras_not_ready")
        self.assertEqual(
            body["speech"],
            "无法开始校准：左手相机没有VIO数据；头部相机没有图像和VIO数据。请检查相机连接。",
        )
        self.assertEqual(
            [item["camera"] for item in body["unavailable_cameras"]],
            ["insight3_b", "insight9_a"],
        )
        self.assertTrue(body["camera_health"]["insight3_a"]["ready"])
        node.reset_mapping.assert_not_called()

    async def test_reset_starts_when_all_camera_inputs_are_fresh(self):
        node = self._node()
        routes = MappingRoutes(SimpleNamespace(node=node))

        response = await routes._handle_reset(None)

        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.text)["ok"])
        node.reset_mapping.assert_called_once_with()

    async def test_reset_unavailable_returns_actionable_speech(self):
        payload = {
            "ok": False,
            "requested": [],
            "unavailable": ["localizer", "mapper"],
            "mapping": {},
        }
        routes = MappingRoutes(SimpleNamespace(node=self._node(reset_payload=payload)))

        response = await routes._handle_reset(None)
        body = json.loads(response.text)

        self.assertEqual(response.status, 503)
        self.assertIn("校准服务未就绪", body["speech"])


if __name__ == "__main__":
    unittest.main()
