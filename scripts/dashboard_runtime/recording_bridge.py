"""Live image-header audit used alongside the native rosbag recorder."""

from __future__ import annotations

from typing import Dict

from post_processing_core.integrity import nominal_for


class RecordingBridge:
    def __init__(self, owner) -> None:
        self.owner = owner

    def start_image_recording(self, topic_output_paths: Dict[str, str]) -> None:
        """Start auditing images already received by the dashboard.

        The native rosbag2 process owns all storage writes. Reusing the existing
        image callbacks here gives us an independent source-timestamp continuity
        check without putting serialization or disk I/O in a ROS callback.
        """
        topics = set(topic_output_paths)
        with self.owner._recording_writer_lock:
            if self.owner._recording_writer_by_topic:
                raise RuntimeError("Image recording audit is already running.")
            self.owner._recording_writer_by_topic = {topic: True for topic in topics}
            self.owner._recording_header_audit = {
                topic: {
                    "count": 0,
                    "first_ns": None,
                    "last_ns": None,
                    "missing": 0,
                    "gap_events": 0,
                    "worst_gap_ns": 0,
                }
                for topic in topics
            }
        # A start-paused rosbag subscription consumes transient-local samples
        # while paused. Publish the cached transform once after resume so the
        # same native writer records /tf_static in the clean recording window.
        republish_tf_static = getattr(self.owner, "republish_tf_static", None)
        if callable(republish_tf_static):
            republish_tf_static()

    def stop_image_recording(self) -> Dict[str, object]:
        with self.owner._recording_writer_lock:
            self.owner._recording_writer_by_topic = {}
            audit = self.owner._finalize_image_header_audit()
            self.owner._recording_header_audit = {}
        return {
            "dropped": 0,
            "dropped_by_topic": {},
            "queue_high_watermark": 0,
            "pending_topics": [],
            "subscription_errors": {},
            "image_header_audit": audit,
        }

    def _finalize_image_header_audit(self) -> Dict[str, object]:
        """Return the in-flight image continuity report without reading the bag."""
        topics: Dict[str, object] = {}
        for topic, stat in self.owner._recording_header_audit.items():
            count = int(stat["count"])
            first_ns = stat["first_ns"]
            last_ns = stat["last_ns"]
            nominal_hz = nominal_for(topic)
            span_s = (
                (int(last_ns) - int(first_ns)) / 1e9
                if count > 1 and first_ns is not None and last_ns is not None
                else 0.0
            )
            topics[topic] = {
                "frames": count,
                "nominal_hz": nominal_hz,
                "observed_hz": round((count - 1) / span_s, 3) if span_s > 0 else None,
                "missing": int(stat["missing"]),
                "gap_events": int(stat["gap_events"]),
                "worst_gap_ms": round(int(stat["worst_gap_ns"]) / 1e6, 3),
                "writer_queue_dropped": 0,
                "ok": count > 1 and int(stat["missing"]) == 0,
            }
        return {
            "method": "live_image_header_audit",
            "topics": topics,
            "ok": bool(topics) and all(item["ok"] for item in topics.values()),
        }

    def snapshot_image_header_audit(self) -> Dict[str, object]:
        """Return an in-flight copy for active QC without stopping capture."""
        with self.owner._recording_writer_lock:
            if not self.owner._recording_header_audit:
                return {"method": "live_image_header_audit", "topics": {}, "ok": True}
            return self._finalize_image_header_audit()

    def _feed_recording_writer(self, topic: str, msg: object) -> None:
        if topic not in self.owner._recording_writer_by_topic:
            return
        header = getattr(msg, "header", None)
        stamp = getattr(header, "stamp", None)
        source_ns = (
            int(getattr(stamp, "sec", 0)) * 1_000_000_000
            + int(getattr(stamp, "nanosec", 0))
        )
        if source_ns <= 0:
            return

        audit = self.owner._recording_header_audit.get(topic)
        if audit is None:
            return
        previous_ns = audit["last_ns"]
        if previous_ns is not None:
            gap_ns = source_ns - int(previous_ns)
            nominal_hz = nominal_for(topic)
            if nominal_hz and gap_ns > 1.5e9 / nominal_hz:
                audit["gap_events"] = int(audit["gap_events"]) + 1
                audit["missing"] = int(audit["missing"]) + max(
                    0, round(gap_ns * nominal_hz / 1e9) - 1
                )
                audit["worst_gap_ns"] = max(int(audit["worst_gap_ns"]), gap_ns)
        if audit["first_ns"] is None:
            audit["first_ns"] = source_ns
        audit["last_ns"] = source_ns
        audit["count"] = int(audit["count"]) + 1
