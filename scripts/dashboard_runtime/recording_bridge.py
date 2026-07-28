"""In-process image writer lifecycle and live header audit."""

from __future__ import annotations

from typing import Dict

from post_processing_core.integrity import nominal_for
from inprocess_bag_writer import InProcessBagWriter
from post_processing import STORAGE_CONFIG_PATH


class RecordingBridge:
    def __init__(self, owner) -> None:
        self.owner = owner

    def start_image_recording(self, topic_output_paths: Dict[str, str]) -> None:
        """Start one image writer per output path to avoid cross-camera blocking."""
        with self.owner._recording_writer_lock:
            if self.owner._recording_writers:
                raise RuntimeError("Image recording writer is already running.")
            writers_by_path: Dict[str, InProcessBagWriter] = {}
            writer_by_topic: Dict[str, InProcessBagWriter] = {}
            for topic, output_path in topic_output_paths.items():
                writer = writers_by_path.get(output_path)
                if writer is None:
                    # Depth 512 covers measured transient SQLite/writeback stalls.
                    writer = InProcessBagWriter(
                        output_path,
                        max_queue=512,
                        storage_config_uri=str(STORAGE_CONFIG_PATH) if STORAGE_CONFIG_PATH.is_file() else "",
                    )
                    writers_by_path[output_path] = writer
                writer_by_topic[topic] = writer
            self.owner._recording_writers = writers_by_path
            self.owner._recording_writer_by_topic = writer_by_topic
            self.owner._recording_timestamp_offsets_ns = {topic: None for topic in topic_output_paths}
            self.owner._recording_header_audit = {
                topic: {"count": 0, "first_ns": None, "last_ns": None, "missing": 0,
                        "gap_events": 0, "worst_gap_ns": 0}
                for topic in topic_output_paths
            }

    def stop_image_recording(self) -> Dict[str, object]:
        with self.owner._recording_writer_lock:
            writers = self.owner._recording_writers
            self.owner._recording_writers = {}
            self.owner._recording_writer_by_topic = {}
            self.owner._recording_timestamp_offsets_ns = {}
            audit = self.owner._finalize_image_header_audit()
            self.owner._recording_header_audit = {}
        dropped = 0
        dropped_by_topic: Dict[str, int] = {}
        for writer in writers.values():
            writer.close()
            dropped += writer.dropped_count
            for topic, count in writer.dropped_by_topic.items():
                dropped_by_topic[topic] = dropped_by_topic.get(topic, 0) + int(count)
        # Include disk-queue drops that header continuity cannot detect.
        audit["writer_queue_dropped"] = dropped
        audit["writer_queue_dropped_by_topic"] = dropped_by_topic
        for topic, topic_audit in audit.get("topics", {}).items():
            topic_dropped = int(dropped_by_topic.get(topic, 0))
            topic_audit["writer_queue_dropped"] = topic_dropped
            topic_audit["ok"] = bool(topic_audit.get("ok")) and topic_dropped == 0
        audit["ok"] = bool(audit["ok"]) and dropped == 0
        return {"dropped": dropped, "image_header_audit": audit}

    def _finalize_image_header_audit(self) -> Dict[str, object]:
        """Return the in-flight image continuity report without re-reading a bag."""
        topics: Dict[str, object] = {}
        for topic, stat in self.owner._recording_header_audit.items():
            count = int(stat["count"])
            first_ns = stat["first_ns"]
            last_ns = stat["last_ns"]
            nominal_hz = nominal_for(topic)
            span_s = ((int(last_ns) - int(first_ns)) / 1e9) if count > 1 and first_ns is not None and last_ns is not None else 0.0
            topics[topic] = {
                "frames": count,
                "nominal_hz": nominal_hz,
                "observed_hz": round((count - 1) / span_s, 3) if span_s > 0 else None,
                "missing": int(stat["missing"]),
                "gap_events": int(stat["gap_events"]),
                "worst_gap_ms": round(int(stat["worst_gap_ns"]) / 1e6, 3),
                "ok": count > 1 and int(stat["missing"]) == 0,
            }
        return {"method": "live_image_header_audit", "topics": topics,
                "ok": bool(topics) and all(item["ok"] for item in topics.values())}

    def _feed_recording_writer(self, topic: str, msg: object) -> None:
        writer = self.owner._recording_writer_by_topic.get(topic)
        if writer is None:
            return
        now_ns = self.owner.get_clock().now().nanoseconds
        header = getattr(msg, "header", None)
        stamp = getattr(header, "stamp", None)
        source_ns = int(getattr(stamp, "sec", 0)) * 1_000_000_000 + int(getattr(stamp, "nanosec", 0))
        if source_ns <= 0:
            # Keep malformed or headerless custom messages recordable.
            writer.write(topic, msg, now_ns)
            return

        audit = self.owner._recording_header_audit.get(topic)
        if audit is not None:
            previous_ns = audit["last_ns"]
            if previous_ns is not None:
                gap_ns = source_ns - int(previous_ns)
                nominal_hz = nominal_for(topic)
                if nominal_hz and gap_ns > 1.5e9 / nominal_hz:
                    audit["gap_events"] = int(audit["gap_events"]) + 1
                    audit["missing"] = int(audit["missing"]) + max(0, round(gap_ns * nominal_hz / 1e9) - 1)
                    audit["worst_gap_ns"] = max(int(audit["worst_gap_ns"]), gap_ns)
            if audit["first_ns"] is None:
                audit["first_ns"] = source_ns
            audit["last_ns"] = source_ns
            audit["count"] = int(audit["count"]) + 1

        offset_ns = self.owner._recording_timestamp_offsets_ns.get(topic)
        if offset_ns is None:
            # Anchor boot-relative clocks once; retain source cadence thereafter.
            offset_ns = now_ns - source_ns
            self.owner._recording_timestamp_offsets_ns[topic] = offset_ns
        writer.write(topic, msg, source_ns + offset_ns)
