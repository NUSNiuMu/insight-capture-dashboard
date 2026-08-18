import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.api.routes.mapping import MappingRoutes  # noqa: E402


class MappingRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_reset_unavailable_returns_actionable_speech(self):
        payload = {
            "ok": False,
            "requested": [],
            "unavailable": ["localizer", "mapper"],
            "mapping": {},
        }
        routes = MappingRoutes(
            SimpleNamespace(node=SimpleNamespace(reset_mapping=lambda: payload))
        )

        response = await routes._handle_reset(None)
        body = json.loads(response.text)

        self.assertEqual(response.status, 503)
        self.assertIn("校准服务未就绪", body["speech"])


if __name__ == "__main__":
    unittest.main()
