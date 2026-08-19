import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.api.routes.settings import SettingsRoutes  # noqa: E402


class _JsonRequest:
    can_read_body = True

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class SettingsRoutesTest(unittest.IsolatedAsyncioTestCase):
    def _routes(self):
        node = SimpleNamespace(
            build_settings_payload=lambda: {"poses": []},
        )
        return SettingsRoutes(SimpleNamespace(node=node))

    async def test_settings_include_live_voice_audio_status(self):
        routes = self._routes()
        status = {
            "available": True,
            "volume_percent": 45,
            "backend": "pulse",
            "pulse_sink": "alsa_output.usb-device",
        }
        with mock.patch.object(
            routes,
            "_voice_control_request",
            new=mock.AsyncMock(return_value=status),
        ):
            response = await routes._handle_settings_get(None)

        self.assertEqual(json.loads(response.text)["voice_audio"], status)

    async def test_settings_stay_available_when_voice_service_is_offline(self):
        routes = self._routes()
        with mock.patch.object(
            routes,
            "_voice_control_request",
            new=mock.AsyncMock(side_effect=RuntimeError("offline")),
        ):
            response = await routes._handle_settings_get(None)

        payload = json.loads(response.text)
        self.assertEqual(payload["poses"], [])
        self.assertFalse(payload["voice_audio"]["available"])

    async def test_volume_update_is_proxied_to_host_voice_service(self):
        routes = self._routes()
        status = {"available": True, "volume_percent": 32}
        control = mock.AsyncMock(return_value=status)
        with mock.patch.object(routes, "_voice_control_request", new=control):
            response = await routes._handle_settings_voice_volume(
                _JsonRequest({"volume_percent": 32})
            )

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["voice_audio"], status)
        control.assert_awaited_once_with(
            "/v1/audio/volume",
            payload={"volume_percent": 32},
        )

    async def test_sample_playback_allows_for_cold_tts_startup(self):
        routes = self._routes()
        status = {"available": True, "volume_percent": 40}
        control = mock.AsyncMock(return_value=status)
        with mock.patch.object(routes, "_voice_control_request", new=control):
            response = await routes._handle_settings_voice_sample(None)

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["voice_audio"], status)
        control.assert_awaited_once_with(
            "/v1/audio/sample",
            payload={},
            timeout_sec=15.0,
        )


if __name__ == "__main__":
    unittest.main()
