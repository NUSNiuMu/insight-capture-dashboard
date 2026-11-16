import sys
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from insight_capture.runtime.mapping.stream import MappingStream


class MappingStreamResetTest(unittest.TestCase):
    def setUp(self):
        self.request_patch = patch('insight_capture.runtime.mapping.stream.Empty',
                                   SimpleNamespace(Request=object))
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.owner = SimpleNamespace(clear_traces=Mock())
        self.stream = MappingStream(self.owner)

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
        self.owner.clear_traces.assert_called_once_with()

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


    def test_dispatch_notification_precedes_response_wait(self):
        future = Future()
        observed = []
        self.stream._reset_client = SimpleNamespace(service_is_ready=lambda: True, call_async=lambda _: future)
        result = self.stream.request_reset(.001, on_dispatched=lambda: observed.append(future.done()))
        self.assertEqual(observed, [False])
        self.assertEqual(result['error'], 'mapping_reset_timeout')
        self.owner.clear_traces.assert_not_called()

    def test_unavailable_service_notifies_admission_complete(self):
        notification = Mock()
        self.assertFalse(self.stream.request_reset(on_dispatched=notification)['ok'])
        notification.assert_called_once_with()

    def test_dispatch_error_notifies_admission_complete(self):
        notification = Mock()
        self.stream._reset_client = SimpleNamespace(service_is_ready=lambda: True,
            call_async=Mock(side_effect=RuntimeError('dispatch failed')))
        with self.assertRaisesRegex(RuntimeError, 'dispatch failed'):
            self.stream.request_reset(on_dispatched=notification)
        notification.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
