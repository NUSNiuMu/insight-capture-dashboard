import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from insight_capture.postprocess.bags import integrity


TOPIC = "/insight9_a/camera/vio_100hz"


def packet(stamp):
    sec, nsec = divmod(stamp, 1_000_000_000)
    return b"\x00\x01\x00\x00" + struct.pack("<iI", sec, nsec)


class IntegrityTest(unittest.TestCase):
    def analyze(self, stamps, *, raw=None, deep=True, record_start=1_000_000_000, record_period=10_000_000):
        rows = [(TOPIC, blob, record_start + i * record_period)
                for i, blob in enumerate(raw if raw is not None else map(packet, stamps))]
        windows = {TOPIC: {"msgs": len(rows), "first_ns": rows[0][2], "last_ns": rows[-1][2]}}
        with patch.object(integrity, "_read_topic_windows", return_value=windows), \
                patch.object(integrity, "single_mcap"), \
                patch.object(integrity, "read_metadata", return_value={
                    "topics_with_message_count": [{"topic_metadata": {"name": TOPIC}}]}), \
                patch.object(integrity, "iter_messages", return_value=iter(rows)), \
                patch.object(integrity, "_selected_topics", return_value=[]):
            return integrity.analyze_bag(Path("/nonexistent/qa"), deep=deep)

    def test_zero_source_clock_cannot_pass_as_complete(self):
        report = self.analyze([0] * 100)
        self.assertFalse(report["ok"])
        self.assertEqual(report["topics"][0]["timestamp_diagnostics"]["invalid_source_stamps"], 100)

    def test_malformed_header_cannot_silently_become_recorder_time(self):
        report = self.analyze([], raw=[b"bad"] * 100)
        self.assertFalse(report["ok"])
        self.assertEqual(report["topics"][0]["timestamp_diagnostics"]["invalid_source_stamps"], 100)

    def test_nanoseconds_outside_ros_time_range_are_invalid(self):
        bad = b"\x00\x01\x00\x00" + struct.pack("<iI", 1, 1_000_000_000)
        report = self.analyze([], raw=[packet(1_000_000_000), bad, packet(1_020_000_000)])
        self.assertFalse(report["ok"])
        self.assertEqual(report["topics"][0]["timestamp_diagnostics"]["invalid_source_stamps"], 1)

    def test_repeated_only_source_clock_cannot_pass(self):
        report = self.analyze([1_000_000_000] * 100)
        self.assertFalse(report["ok"])
        self.assertIn("distinct", report["topics"][0]["error"])

    def test_duplicate_and_backward_stamps_remain_visible_before_sorting(self):
        stamps = [1_000_000_000 + i * 10_000_000 for i in range(100)]
        stamps[3], stamps[4] = stamps[4], stamps[3]
        stamps.insert(50, stamps[49])
        topic = self.analyze(stamps)["topics"][0]
        self.assertTrue(topic["ok"])  # Diagnostics do not add a capture or rate gate.
        self.assertEqual({key: topic["timestamp_diagnostics"][key] for key in (
            'valid_source_stamps', 'unique_source_stamps', 'duplicate_source_stamps',
            'backward_steps', 'invalid_source_stamps')}, {
            "valid_source_stamps": 101, "unique_source_stamps": 100,
            "duplicate_source_stamps": 1, "backward_steps": 1,
            "invalid_source_stamps": 0,
        })
        self.assertTrue(topic["warnings"])

    def test_wrong_clock_domain_is_not_billions_of_missing_messages(self):
        epoch = 1_794_487_704_000_000_000
        stamps = [epoch + i * 10_000_000 for i in range(100)]
        stamps[20:30] = [18_112_000_000_000 + i * 10_000_000 for i in range(10)]
        topic = self.analyze(stamps, record_start=epoch)["topics"][0]
        self.assertFalse(topic['ok'])
        self.assertFalse(topic['rate_assessable'])
        self.assertEqual(topic['msgs'], 100)
        self.assertEqual(topic['timestamp_diagnostics']['source_record_time_mismatches'], 10)
        self.assertIsNone(topic['missing'])
        self.assertIsNone(topic['loss_pct'])
        self.assertIsNone(topic['avg_hz'])

    def test_large_real_gap_in_both_clocks_still_reports_rate_shortfall(self):
        # Receipt and source both advance: this is not an epoch change.
        stamps = [1_000_000_000, 11_000_000_000]
        topic = self.analyze(stamps, record_period=10_000_000_000)['topics'][0]
        self.assertTrue(topic['rate_assessable'])
        self.assertGreater(topic['missing'], 900)

    def test_source_clock_offset_bounds_match_existing_live_check(self):
        for offset, good in [(-2_000_000_000, True), (1_000_000_000, True),
                             (-2_000_000_001, False), (1_000_000_001, False)]:
            with self.subTest(offset=offset):
                stamps = [5_000_000_000 + offset + i * 10_000_000 for i in range(100)]
                topic = self.analyze(stamps, record_start=5_000_000_000)['topics'][0]
                self.assertEqual(topic['ok'], good)

    def test_big_endian_source_header_is_decoded_in_its_declared_order(self):
        raw = [b'\x00\x00\x00\x00' + struct.pack('>iI', 1, i * 10_000_000) for i in range(100)]
        self.assertTrue(self.analyze([], raw=raw)['ok'])

    def test_unsupported_cdr_header_is_reported_without_guessing_a_clock(self):
        topic = self.analyze([], raw=[b'\x00\x09\x00\x00' + struct.pack('<iI', 1, 0)])['topics'][0]
        self.assertFalse(topic['rate_assessable'])
        self.assertEqual(topic['timestamp_diagnostics']['invalid_source_stamps'], 1)

    def test_source_rate_shortfall_is_not_transport_loss_attribution(self):
        stamps = [1_000_000_000 + i * 10_200_000 for i in range(1000)]
        report = self.analyze(stamps)
        topic = report["topics"][0]
        self.assertFalse(topic["ok"])
        self.assertEqual(topic["nominal_hz"], 99)
        self.assertEqual(topic["missing"], 9)
        self.assertEqual(topic["loss_attribution"], "undetermined")
        self.assertEqual(report["max_loss_pct"], 0.5)
        self.assertIn("source", report["interpretation"])

    def test_quick_report_discloses_uninspected_source_clock(self):
        report = self.analyze([0] * 100, deep=False)
        self.assertTrue(report["ok"])
        self.assertFalse(report["source_timestamps_inspected"])
        self.assertEqual(report["topics"][0]["loss_attribution"], "undetermined")


if __name__ == "__main__":
    unittest.main()
