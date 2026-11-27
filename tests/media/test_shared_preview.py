from array import array
import time

import pytest

from insight_capture.media.shared_preview import PreviewReader, PreviewWriter, shared_preview_topics


def test_shared_preview_keeps_latest_frame_and_reopens_after_encoder_restart(tmp_path):
    path = tmp_path / "wrist"
    reader = PreviewReader(path)
    start = time.monotonic_ns()
    writer = PreviewWriter(path, 4, 2)
    try:
        assert reader.take(start + 1) is None
        assert writer.offer(array("B", [33] * 12), 1_000_000_008, start + 2)
        assert writer.offer(array("B", [66] * 12), 1_000_000_009, start + 3)
        frame = reader.take(start + 4)
        assert (frame.width, frame.height, frame.step, frame.encoding) == (4, 2, 4, "nv12")
        assert (frame.header.stamp.sec, frame.header.stamp.nanosec) == (1, 9)
        assert frame.data == bytes([66] * 12)
        assert reader.take(start + 5) is None
        writer.close()

        writer = PreviewWriter(path, 2, 2)
        assert writer.offer(bytes([22] * 6), 2_000_000_000, start + 300_000_000)
        frame = reader.take(start + 301_000_000)
        assert frame.data == bytes([22] * 6)
        assert (frame.width, frame.height) == (2, 2)
    finally:
        writer.close()
        reader.close()
    assert not path.exists()


def test_shared_preview_rejects_stale_frames_and_non_encoder_topics(tmp_path):
    path = tmp_path / "wrist"
    writer = PreviewWriter(path, 4, 2)
    reader = PreviewReader(path)
    try:
        start = time.monotonic_ns()
        assert writer.offer(bytes(12), 1, start)
        assert reader.take(start + 151_000_000) is None
    finally:
        reader.close()
        writer.close()

    assert shared_preview_topics({}) == set()
    assert shared_preview_topics({"input_topics": ["/wrist"], "shared_preview_topics": ["/wrist"]}) == {"/wrist"}
    with pytest.raises(ValueError, match="recording encoder inputs"):
        shared_preview_topics({"shared_preview_topics": ["/other"]})


def test_shared_preview_does_not_open_symlinks(tmp_path):
    actual = tmp_path / "actual"
    alias = tmp_path / "alias"
    writer = PreviewWriter(actual, 4, 2)
    reader = PreviewReader(alias)
    try:
        alias.symlink_to(actual)
        with pytest.raises(OSError):
            reader.take()
    finally:
        reader.close()
        writer.close()
