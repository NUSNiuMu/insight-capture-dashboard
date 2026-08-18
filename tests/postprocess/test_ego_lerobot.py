from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

import numpy as np


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from insight_capture.postprocess.datasets.ego_lerobot.identity import temporal_identity_corrections  # noqa: E402
from insight_capture.postprocess.datasets.ego_lerobot.model import load_backend  # noqa: E402
from insight_capture.postprocess.datasets.ego_lerobot.spec import load_spec, validate_segments  # noqa: E402


class EgoLeRobotTest(unittest.TestCase):
    def test_temporal_identity_correction_moves_to_closer_track(self) -> None:
        positions = np.zeros((2, 2, 3), dtype=np.float64)
        positions[0, 1] = [1.0, 0.0, 0.0]
        positions[1, 0] = [1.01, 0.0, 0.0]
        valid = np.asarray([[True, True], [True, False]])
        corrections = temporal_identity_corrections(positions, valid)
        self.assertEqual(len(corrections), 1)
        self.assertEqual((corrections[0].source_hand, corrections[0].target_hand), (0, 1))

    def test_external_backend_factory(self) -> None:
        module = types.ModuleType("test_external_hand_backend")
        backend_type = type(
            "Backend",
            (),
            {
                "name": "test",
                "version": "1",
                "cache_identity": lambda self: {"version": "1"},
                "predict": lambda self, image: [],
                "close": lambda self: None,
            },
        )
        module.make = lambda **kwargs: backend_type()
        sys.modules[module.__name__] = module
        backend = load_backend(f"{module.__name__}:make", focal_length=1.0)
        self.assertEqual(backend.name, "test")

    def test_segment_coverage(self) -> None:
        payload = {
            "schema_version": 1,
            "dataset_id": "test/dataset",
            "task": "test",
            "fps": 30,
            "crop": {"start_s": 0, "end_s": 1},
            "segments": [
                {"subtask": "one", "atomic_action": "a", "task": "first", "start_frame": 0, "end_frame": 1},
                {"subtask": "two", "atomic_action": "b", "task": "second", "start_frame": 2, "end_frame": 3},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            spec = load_spec(path)
        validate_segments(spec.segments, 4)
        with self.assertRaises(ValueError):
            validate_segments(spec.segments, 5)


if __name__ == "__main__":
    unittest.main()
