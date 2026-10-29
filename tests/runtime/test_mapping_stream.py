import sys
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.runtime.mapping.stream import MappingStream


class MappingStreamResetTest(unittest.TestCase):
    def setUp(self):
        self.request_patch = patch('insight_capture.runtime.mapping.stream.Empty',
                                   SimpleNamespace(Request=object))
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.stream = MappingStream(object())

    def test_waits_for_one_reset_response(self):
        future = Future()
        calls = []

        def call(request):
            calls.append(request)
            threading.Timer(.01, lambda: future.set_result(object())).start()
            return future

        self.stream._reset_client = SimpleNamespace(service_is_ready=lambda: True, call_async=call)
        self.assertTrue(self.stream.request_reset()['ok'])
        self.assertTrue(future.done())
        self.assertEqual(len(calls), 1)

    def test_timeout_does_not_claim_success(self):
        self.stream._reset_client = SimpleNamespace(service_is_ready=lambda: True,
                                                   call_async=lambda _: Future())
        result = self.stream.request_reset(.001)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'mapping_reset_timeout')

    def test_unavailable_service(self):
        self.assertFalse(self.stream.request_reset()['ok'])

    def test_service_error_is_preserved(self):
        future = Future()
        future.set_exception(RuntimeError('engine unavailable'))
        self.stream._reset_client = SimpleNamespace(service_is_ready=lambda: True,
                                                   call_async=lambda _: future)
        self.assertEqual(self.stream.request_reset()['error'], 'engine unavailable')


if __name__ == '__main__':
    unittest.main()
