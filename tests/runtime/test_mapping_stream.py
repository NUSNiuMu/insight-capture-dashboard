import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import insight_capture.runtime.mapping.stream as mapping_stream_module  # noqa: E402
from insight_capture.runtime.mapping.stream import MappingStream  # noqa: E402


class _Trigger:
    class Request:
        pass


class _Future:
    def __init__(self, response):
        self.response = response

    def done(self):
        return True

    def result(self):
        return self.response


class _Client:
    def __init__(self, response, ready=True):
        self.response = response
        self.ready = ready

    def service_is_ready(self):
        return self.ready

    def call_async(self, _request):
        return _Future(self.response)


class _Response:
    def __init__(self, success, message):
        self.success = success
        self.message = message


class MappingStreamCaptureReferenceTest(unittest.TestCase):
    def setUp(self):
        self.original_trigger = mapping_stream_module.Trigger
        mapping_stream_module.Trigger = _Trigger
        self.stream = MappingStream(object())

    def tearDown(self):
        mapping_stream_module.Trigger = self.original_trigger

    def test_returns_frozen_reference_payload(self):
        response = _Response(
            True,
            '{"session_id":"abc","session_generation":2,'
            '"reference_active":true,"reference_id":3,'
            '"reference_keyframe":90,"validation_count":0}',
        )
        self.stream._capture_reference_client = _Client(response)
        result = self.stream.freeze_capture_reference()
        self.assertTrue(result["ok"])
        self.assertEqual(result["reference"]["reference_keyframe"], 90)

    def test_preserves_mapper_rejection_details(self):
        response = _Response(
            False,
            '{"reason":"insufficient_reference_features",'
            '"reference_features":42,"minimum_features":80}',
        )
        self.stream._capture_reference_client = _Client(response)
        result = self.stream.freeze_capture_reference()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "insufficient_reference_features")
        self.assertEqual(result["details"]["reference_features"], 42)


if __name__ == "__main__":
    unittest.main()
