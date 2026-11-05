import json
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from insight_capture.runtime.anomaly import ActiveQcMonitor
from insight_capture.media.image_pipeline import ImagePipeline
from insight_capture.runtime.preflight import CapturePreflight
from insight_capture.runtime.recording.header_audit import RecordingBridge
from insight_capture.media.preview_manager import PreviewManager
from insight_capture.runtime.payloads import PayloadBuilder
from insight_capture.runtime.ros.node import PoseBridgeNode
from insight_capture.runtime.ros.topics import playback_topic
from insight_capture.runtime.take import SessionTakeStore
from insight_capture.runtime.tasks import CaptureTask, CaptureTaskCatalog


class _Manager:
    def __init__(self, root: Path):
        self.rosbag_root = root
        self.default_topics = ["/cam/a", "/cam/b", "/cam/c", "/tf_static"]
        self.storage_status = {"using_fallback": False, "active_path": str(root)}
        self.recording = False
        self.recording_mode = "capture"

    def current_topic_catalog(self, refresh=True):
        return {"topics": list(self.default_topics)}

    def is_recording(self):
        return self.recording

    def status(self):
        return {
            "recording": self.recording,
            "recording_mode": self.recording_mode,
            "storage": self.storage_status,
            "recent_output": [],
        }


def _take_store(root):
    catalog = CaptureTaskCatalog(
        [CaptureTask("fold", "Fold", "Fold", "capture")],
        default_task_id="fold",
    )
    return SessionTakeStore(root, task_catalog=catalog)


class CaptureRuntimeTest(unittest.TestCase):
    def test_playback_topic_is_owned_by_ros_runtime(self):
        self.assertEqual(playback_topic("/camera/image"), "/bagplay/camera/image")

    def test_node_close_releases_owned_media_resources(self):
        calls = []
        node = SimpleNamespace(
            _participant_watchdog=SimpleNamespace(close=lambda: calls.append("watchdog")),
            camera_inputs=SimpleNamespace(close=lambda: calls.append("inputs")),
            _preview_manager=SimpleNamespace(close=lambda: calls.append("preview")),
            stop_webrtc_worker=lambda: calls.append("webrtc"),
        )

        PoseBridgeNode.close(node)

        self.assertEqual(calls, ["watchdog", "inputs", "preview", "webrtc"])

    def test_pose_payload_keeps_configured_3d_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "assets" / "models" / "hand.glb"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"glTF")
            pose = SimpleNamespace(
                name="cam",
                teleop_role="right_hand",
                avatar_model="assets/models/hand.glb",
                avatar_scale=3.0,
                avatar_rotation_deg_xyz=(0.0, 90.0, 0.0),
                avatar_offset_xyz=(0.0, 0.0, 0.0),
            )
            owner = SimpleNamespace(
                project_root=root,
                poses=[pose],
                pose_lock=threading.Lock(),
                trace_generation=1,
                latest_pose_sample={"cam": None},
                local_vio_samples={"cam": {
                    "name": "cam", "position": [1., 2., 3.], "stamp_ns": 42,
                    "quaternion_xyzw": [0., 0., 0., 1.],
                    "generation": 3, "received": time.monotonic(),
                }},
                last_pose_received_time={"cam": 0.0},
                pose_timeout_sec=2.0,
                raw_traces={"cam": deque()},
                raw_trace_sequences={"cam": deque()},
                trace_sequences={"cam": 0},
                display_fps_limit=30.0,
                max_points=100,
            )
            builder = PayloadBuilder(owner)

            payload = builder.build_pose_payload()

            self.assertEqual(payload["poses"][0]["avatar_model"], pose.avatar_model)
            self.assertEqual(payload["poses"][0]["avatar_scale"], 3.0)
            self.assertFalse(payload["poses"][0]["visible"])
            self.assertEqual(payload["poses"][0]["trace_update"]["points"], [])
            self.assertEqual(payload["local_vio"][0]["coordinate_frame"], "local_vio")
            self.assertEqual(payload["local_vio"][0]["position"], [1., 2., 3.])
            self.assertTrue(payload["local_vio"][0]["available"])
            self.assertIn(
                "/asset?path=assets%2Fmodels%2Fhand.glb",
                builder.model_asset_url(pose.avatar_model),
            )

    def test_capture_callback_skips_display_until_viewer(self):
        event = threading.Event()
        owner = SimpleNamespace(
            cameras=[SimpleNamespace(name="cam", topic="/cam/a")],
            _pending_frame_events={"cam": event},
            _playback_mode=False,
            camera_input_lock=threading.Lock(),
            camera_input_times={"cam": deque(maxlen=10)},
            _pending_frames={},
            _localization_image_publishers={},
            viewer=False,
        )
        fed = []
        owner._feed_recording_writer = lambda topic, msg: fed.append((topic, msg))
        owner.preview_requested = lambda: owner.viewer
        msg = SimpleNamespace(header=SimpleNamespace(stamp=SimpleNamespace(sec=1, nanosec=0)))
        callback = ImagePipeline(owner)._make_dashboard_image_callback("cam", "image")

        callback(msg)
        self.assertEqual(fed, [("/cam/a", msg)])
        self.assertFalse(event.is_set())
        self.assertNotIn("cam", owner._pending_frames)

        owner.viewer = True
        callback(msg)
        self.assertTrue(event.is_set())
        self.assertIs(owner._pending_frames["cam"], msg)

    def test_viewer_activity_lazy_starts_worker(self):
        calls = []
        owner = SimpleNamespace(
            _webrtc_has_sessions={},
            _webrtc_proc=None,
            _worker_supervisor=SimpleNamespace(
                ensure_webrtc_worker=lambda: calls.append("start")
            ),
            stop_webrtc_worker=lambda: calls.append("stop"),
        )
        manager = PreviewManager(owner, lease_sec=1.0, idle_stop_sec=1.0)
        try:
            self.assertFalse(manager.requested())
            manager.activity()
            self.assertEqual(calls, ["start"])
            self.assertTrue(manager.requested())
        finally:
            manager.close()

    def test_dashboard_header_audit_is_not_recording_quality_authority(self):
        topic = "/insight3_a/camera/infra1/image_rect_raw"
        owner = SimpleNamespace(
            _recording_header_audit={
                topic: {
                    "count": 2,
                    "first_ns": 1_000_000_000,
                    "last_ns": 2_000_000_000,
                    "missing": 29,
                    "gap_events": 1,
                    "worst_gap_ns": 1_000_000_000,
                }
            }
        )

        audit = RecordingBridge(owner)._finalize_image_header_audit()

        self.assertEqual(audit["scope"], "dashboard_subscription")
        self.assertFalse(audit["recording_quality_authoritative"])
        self.assertFalse(audit["ok"])

    def test_preflight_checks_camera_mapping_storage_and_topics(self):
        with tempfile.TemporaryDirectory() as temporary:
            now = time.monotonic()
            cameras = [
                SimpleNamespace(name=name, label=name, topic=f"/cam/{name}")
                for name in ("a", "b", "c")
            ]
            poses = [
                SimpleNamespace(name=name, topic=f"/pose/{name}")
                for name in ("a", "b", "c")
            ]
            node = SimpleNamespace(
                cameras=cameras,
                poses=poses,
                camera_input_lock=threading.Lock(),
                camera_input_times={name: deque([now - 0.1, now], maxlen=10) for name in ("a", "b", "c")},
                last_pose_received_time={name: now for name in ("a", "b", "c")},
                build_mapping_payload=lambda: {
                    "statuses": {
                        "insight9": {"online": True, "state": "tracking"},
                        "insight3_a": {"online": True, "localized": True},
                        "insight3_b": {"online": True, "localized": True},
                    }
                },
            )
            manager = _Manager(Path(temporary))
            manager.default_topics = [
                "/cam/a", "/cam/b", "/cam/c",
                "/pose/a", "/pose/b", "/pose/c", "/tf_static",
            ]
            report = CapturePreflight(
                node, manager, {"minimum_free_gb": 0}
            ).evaluate()
            self.assertTrue(report["ok"], report)
            manager.storage_status = {
                "using_fallback": True,
                "active_path": str(manager.rosbag_root),
                "fallback_reason": "capture disk is absent",
            }
            report = CapturePreflight(
                node, manager, {"minimum_free_gb": 0}
            ).evaluate()
            self.assertTrue(report["ok"], report)
            self.assertIn("storage_fallback", {item["code"] for item in report["warnings"]})
            self.assertIn("备用存储", CapturePreflight.message(report))
            strict_report = CapturePreflight(
                node,
                manager,
                {"minimum_free_gb": 0, "require_primary_storage": True},
            ).evaluate()
            self.assertFalse(strict_report["ok"])
            self.assertIn(
                "storage_fallback",
                {item["code"] for item in strict_report["failures"]},
            )
            manager.storage_status["using_fallback"] = False
            node.camera_input_times["c"].clear()
            report = CapturePreflight(
                node, manager, {"minimum_free_gb": 0}
            ).evaluate()
            self.assertFalse(report["ok"])
            self.assertIn("camera_stale", {item["code"] for item in report["failures"]})

    def test_reject_take_preserves_raw_bag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _take_store(root)
            take = store.reserve_take()
            bag = root / "bags" / take["bag_name"]
            bag.mkdir(parents=True)
            (bag / "data.mcap").write_bytes(b"raw")
            store.mark_recording(bag)
            rejected = store.reject_current("operator_rejected")
            self.assertFalse(rejected["operator_valid"])
            self.assertTrue((bag / "data.mcap").is_file())

    def test_capture_task_set_persists_counts_across_days_and_reentry(self):
        catalog = CaptureTaskCatalog(
            [
                CaptureTask(
                    task_id="cup_stacking",
                    name="Cup stacking",
                    instruction="Stack the cups",
                    capture_profile="dual_arm_umi",
                )
            ],
            default_task_id="cup_stacking",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionTakeStore(root, task_catalog=catalog)
            first_session = store.session_id
            self.assertEqual(first_session, "cup_stacking")
            self.assertEqual(store.recording_subdirectory(), "cup_stacking")
            take = store.reserve_take()
            self.assertTrue(take["bag_name"].startswith("cup_stacking_take_0001_"))
            store.mark_recording(root / "bags" / take["bag_name"])
            store.complete_current({"output_path": take["bag_name"]})

            status = store.task_status()
            self.assertEqual(status["stats"]["recorded_takes"], 1)
            self.assertEqual(status["stats"]["valid_takes"], 1)
            self.assertEqual(status["stats"]["next_take_id"], 2)

            restored = SessionTakeStore(root, task_catalog=catalog)
            self.assertEqual(restored.session_id, first_session)
            self.assertEqual(restored.task_status()["stats"]["recorded_takes"], 1)
            restored.end_task()
            self.assertFalse(restored.task_status()["active"])
            with self.assertRaisesRegex(RuntimeError, "No capture task"):
                restored.reserve_take()

            restarted = restored.activate_task("cup_stacking")
            self.assertEqual(restarted["session_id"], "cup_stacking")
            self.assertEqual(restarted["stats"]["next_take_id"], 2)
            task_set = restored.list_tasks()[0]
            self.assertTrue(task_set["active"])
            self.assertEqual(task_set["stats"]["recorded_takes"], 1)


    def test_sustained_camera_fault_records_anomaly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _take_store(root)
            store.reserve_take()
            store.mark_recording(root / "bag")
            manager = _Manager(root)
            manager.recording = True
            now = time.monotonic()
            node = SimpleNamespace(
                cameras=[SimpleNamespace(name="a", label="A camera")],
                camera_input_lock=threading.Lock(),
                camera_input_times={"a": deque([], maxlen=10)},
                build_mapping_payload=lambda: {
                    "statuses": {
                        "insight9": {"online": True},
                        "insight3_a": {"online": True, "localized": True},
                        "insight3_b": {"online": True, "localized": True},
                    }
                },
                _recording_bridge=SimpleNamespace(
                    snapshot_image_header_audit=lambda: {"topics": {}}
                ),
            )
            monitor = ActiveQcMonitor(
                node, manager, store,
                {"sustain_sec": 0.5, "minimum_free_gb": 0},
            )
            monitor.poll()
            monitor._pending_since["camera_stale:a"] = now - 1.0
            monitor.poll()
            timeline = store.current()["anomaly_timeline"]
            self.assertEqual(timeline[0]["code"], "camera_stale:a")
            self.assertTrue(timeline[0]["affects_quality"])

    def test_dashboard_reader_gap_does_not_alert_or_mark_take_suspect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _take_store(root)
            store.reserve_take()
            store.mark_recording(root / "bag")
            manager = _Manager(root)
            manager.recording = True
            manager.image_header_audit = {
                "scope": "dashboard_subscription",
                "recording_quality_authoritative": False,
                "ok": False,
                "topics": {"/cam/a": {"missing": 100}},
            }
            manager.status = lambda: {
                "recording": manager.recording,
                "recording_mode": manager.recording_mode,
                "storage": manager.storage_status,
                "recent_output": [],
                "merge_state": "done",
                "network_audit": {"ok": True},
                "image_header_audit": manager.image_header_audit,
                "output_path": str(root / "bag"),
            }
            now = time.monotonic()
            node = SimpleNamespace(
                cameras=[SimpleNamespace(name="a", label="A camera")],
                camera_input_lock=threading.Lock(),
                camera_input_times={"a": deque([now], maxlen=10)},
                build_mapping_payload=lambda: {
                    "statuses": {
                        "insight9": {"online": True},
                        "insight3_a": {"online": True, "localized": True},
                        "insight3_b": {"online": True, "localized": True},
                    }
                },
                _recording_bridge=SimpleNamespace(
                    snapshot_image_header_audit=lambda: manager.image_header_audit
                ),
            )
            monitor = ActiveQcMonitor(
                node, manager, store,
                {"sustain_sec": 0.5, "minimum_free_gb": 0},
            )

            monitor.poll()
            monitor.poll()

            self.assertEqual(store.current()["anomaly_timeline"], [])
            manager.recording = False
            completed = store.complete_current(manager.status())
            self.assertEqual(completed["quick_qc"]["state"], "pass")

    def test_one_off_authoritative_gap_is_not_sustained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _take_store(root)
            store.reserve_take()
            store.mark_recording(root / "bag")
            manager = _Manager(root)
            manager.recording = True
            now = time.monotonic()
            audit = {
                "recording_quality_authoritative": True,
                "topics": {"/cam/a": {"missing": 1}},
            }
            node = SimpleNamespace(
                cameras=[SimpleNamespace(name="a", label="A camera")],
                camera_input_lock=threading.Lock(),
                camera_input_times={"a": deque([now], maxlen=10)},
                build_mapping_payload=lambda: {
                    "statuses": {
                        "insight9": {"online": True},
                        "insight3_a": {"online": True, "localized": True},
                        "insight3_b": {"online": True, "localized": True},
                    }
                },
                _recording_bridge=SimpleNamespace(
                    snapshot_image_header_audit=lambda: audit
                ),
            )
            monitor = ActiveQcMonitor(
                node, manager, store,
                {"sustain_sec": 0.5, "minimum_free_gb": 0},
            )

            monitor.poll()
            monitor._pending_since["frame_loss:/cam/a"] = now - 1.0
            monitor.poll()

            self.assertEqual(store.current()["anomaly_timeline"], [])

    def test_vio_calibration_qc_ignores_missing_rectified_dashboard_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = _Manager(root)
            manager.recording = True
            manager.recording_mode = "vio_calibration"
            node = SimpleNamespace(
                cameras=[
                    SimpleNamespace(name="insight3_a", label="Insight3 A"),
                    SimpleNamespace(name="insight3_b", label="Insight3 B"),
                    SimpleNamespace(name="insight9_a", label="Insight9 A"),
                ],
                camera_input_lock=threading.Lock(),
                camera_input_times={
                    "insight3_a": deque(maxlen=10),
                    "insight3_b": deque(maxlen=10),
                    "insight9_a": deque(maxlen=10),
                },
                build_mapping_payload=lambda: (_ for _ in ()).throw(
                    AssertionError("mapping must not be checked")
                ),
                _recording_bridge=SimpleNamespace(
                    snapshot_image_header_audit=lambda: {"topics": {}}
                ),
            )
            store = SimpleNamespace(
                add_anomaly=lambda _item: (_ for _ in ()).throw(
                    AssertionError("rectified stream must not create an anomaly")
                )
            )
            monitor = ActiveQcMonitor(
                node,
                manager,
                store,
                {"sustain_sec": 0.5, "minimum_free_gb": 0},
            )
            monitor._pending_since["camera_stale:insight3_a"] = 1.0
            monitor._active.add("localization:insight3_b")

            monitor.poll()

            self.assertNotIn("camera_stale:insight3_a", monitor._pending_since)
            self.assertNotIn("localization:insight3_b", monitor._active)

    def test_fallback_storage_warning_does_not_make_take_suspect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _take_store(root)
            store.reserve_take()
            store.mark_recording(root / "bag")
            manager = _Manager(root)
            manager.recording = True
            manager.storage_status = {
                "using_fallback": True,
                "fallback_reason": "capture disk is absent",
            }
            now = time.monotonic()
            node = SimpleNamespace(
                cameras=[SimpleNamespace(name="a", label="A camera")],
                camera_input_lock=threading.Lock(),
                camera_input_times={"a": deque([now], maxlen=10)},
                build_mapping_payload=lambda: {
                    "statuses": {
                        "insight9": {"online": True},
                        "insight3_a": {"online": True, "localized": True},
                        "insight3_b": {"online": True, "localized": True},
                    }
                },
                _recording_bridge=SimpleNamespace(
                    snapshot_image_header_audit=lambda: {"topics": {}}
                ),
            )
            monitor = ActiveQcMonitor(
                node, manager, store,
                {"sustain_sec": 0.5, "minimum_free_gb": 0},
            )
            monitor.poll()
            monitor._pending_since["storage_fallback"] = now - 1.0
            monitor.poll()

            warning = store.current()["anomaly_timeline"][0]
            self.assertEqual(warning["code"], "storage_fallback")
            self.assertEqual(warning["severity"], "warning")
            self.assertFalse(warning["affects_quality"])
            completed = store.complete_current(manager.status())
            self.assertEqual(completed["quick_qc"]["state"], "pass")


if __name__ == "__main__":
    unittest.main()
