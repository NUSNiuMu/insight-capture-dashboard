import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.voice.control import (  # noqa: E402
    load_playback_volume,
    save_playback_volume,
    validate_playback_volume,
)


class VoiceControlSettingsTest(unittest.TestCase):
    def test_volume_validation_accepts_only_integer_percentages(self):
        self.assertEqual(validate_playback_volume(0), 0)
        self.assertEqual(validate_playback_volume("45"), 45)
        self.assertEqual(validate_playback_volume(100.0), 100)
        for invalid in (True, -1, 101, 10.5, "loud"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_playback_volume(invalid)

    def test_volume_is_persisted_atomically_and_other_settings_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text('{"other": true, "playback_volume": 25}\n')

            self.assertEqual(load_playback_volume(path), 25)
            self.assertEqual(save_playback_volume(path, 45), 45)
            self.assertEqual(load_playback_volume(path), 45)
            self.assertEqual(json.loads(path.read_text())["other"], True)

    def test_missing_or_corrupt_settings_use_startup_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            self.assertEqual(load_playback_volume(path, default=40), 40)
            path.write_text("not-json")
            self.assertEqual(load_playback_volume(path, default=35), 35)

if __name__ == "__main__":
    unittest.main()
