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

from insight_capture.runtime.anomaly import ActiveQcMonitor, VoiceAlertQueue
from insight_capture.media.image_pipeline import ImagePipeline
from insight_capture.runtime.preflight import CapturePreflight
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

    def current_topic_catalog(self, refresh=True):
        return {"topics": list(self.default_topics)}

    def is_recording(self):
        return self.recording

    def status(self):
        return {"recording": self.recording, "storage": self.storage_status, "recent_output": []}


class CaptureRuntimeTest(unittest.TestCase):
    def test_playback_topic_is_owned_by_ros_runtime(self):
        self.assertEqual(playback_topic("/camera/image"), "/bagplay/camera/image")

    def test_node_close_releases_owned_media_resources(self):
        calls = []
        node = SimpleNamespace(
            _preview_manager=SimpleNamespace(close=lambda: calls.append("preview")),
            stop_webrtc_worker=lambda: calls.append("webrtc"),
            stop_hand_overlay_worker=lambda: calls.append("hand_overlay"),
        )

        PoseBridgeNode.close(node)

        self.assertEqual(calls, ["preview", "webrtc", "hand_overlay"])

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
                last_pose_received_time={"cam": 0.0},
                fake_pose=True,
                pose_timeout_sec=2.0,
                raw_traces={"cam": deque()},
                raw_trace_sequences={"cam": deque()},
                trace_sequences={"cam": 0},
                display_fps_limit=30.0,
                max_points=100,
                gripper_opening_percent=lambda _name: None,
            )
            builder = PayloadBuilder(owner)

            payload = builder.build_pose_payload()

            self.assertEqual(payload["poses"][0]["avatar_model"], pose.avatar_model)
            self.assertEqual(payload["poses"][0]["avatar_scale"], 3.0)
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
                fake_pose=False,
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
            self.assertIn("备用存储", CapturePreflight.speech(report))
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
            store = SessionTakeStore(root, {"session_id": "s1", "task": "fold"})
            take = store.reserve_take()
            bag = root / "bags" / take["bag_name"]
            bag.mkdir(parents=True)
            (bag / "data.mcap").write_bytes(b"raw")
            store.mark_recording(bag)
            rejected = store.reject_current("operator_rejected")
            self.assertFalse(rejected["operator_valid"])
            self.assertTrue((bag / "data.mcap").is_file())

    def test_capture_task_session_persists_counts_and_starts_new_batch(self):
        catalog = CaptureTaskCatalog(
            [
                CaptureTask(
                    task_id="cup_stacking",
                    name="Cup stacking",
                    speech_name="叠杯子",
                    instruction="Stack the cups",
                    capture_profile="dual_arm_umi",
                    voice_aliases=("叠杯子",),
                    station_check_after_take=True,
                )
            ],
            default_task_id="cup_stacking",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionTakeStore(root, task_catalog=catalog)
            first_session = store.session_id
            self.assertRegex(first_session, r"^\d{8}-cup_stacking-001$")
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
            self.assertRegex(restarted["session_id"], r"^\d{8}-cup_stacking-002$")
            self.assertEqual(restarted["stats"]["next_take_id"], 1)

    def test_sustained_camera_fault_records_anomaly_and_voice_alert(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = SessionTakeStore(root, {"session_id": "s1"})
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
            alerts = VoiceAlertQueue()
            monitor = ActiveQcMonitor(
                node, manager, store, alerts,
                {"sustain_sec": 0.5, "minimum_free_gb": 0},
            )
            monitor.poll()
            monitor._pending_since["camera_stale:a"] = now - 1.0
            monitor.poll()
            self.assertEqual(alerts.since(0)[0]["code"], "camera_stale:a")
            timeline = store.current()["anomaly_timeline"]
            self.assertEqual(timeline[0]["code"], "camera_stale:a")


if __name__ == "__main__":
    unittest.main()
