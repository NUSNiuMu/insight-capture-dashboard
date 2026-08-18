from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectStructureTest(unittest.TestCase):
    def test_scripts_contains_only_field_entry_points(self) -> None:
        expected = {
            "check_bag.py",
            "export_lerobot.py",
            "reboot_cameras.sh",
            "run_dashboard.sh",
            "run_voice.sh",
            "select_device.sh",
            "sync_camera_restart.py",
        }
        actual = {path.name for path in (ROOT / "scripts").iterdir() if path.is_file()}
        self.assertEqual(actual, expected)

    def test_web_workers_use_package_modules(self) -> None:
        from insight_capture.media.worker_supervisor import WorkerSupervisor
        from insight_capture.web.gripper_extraction import GripperExtractionManager
        from insight_capture.web.scoring import ScoringManager
        from insight_capture.web.umi_export import UmiExportManager

        self.assertEqual(
            GripperExtractionManager._MODULE,
            "insight_capture.postprocess.gripper.extraction",
        )
        self.assertEqual(
            ScoringManager._TRAJ_SCORE_MODULE,
            "insight_capture.postprocess.quality.trajectory_score",
        )
        self.assertTrue(UmiExportManager._UMI_MODULE.startswith("insight_capture."))
        self.assertTrue(UmiExportManager._LEROBOT_MODULE.startswith("insight_capture."))
        self.assertTrue(UmiExportManager._EGO_LEROBOT_MODULE.startswith("insight_capture."))
        self.assertEqual(
            WorkerSupervisor._WEBRTC_MODULE,
            "insight_capture.media.webrtc_worker",
        )
        self.assertEqual(
            WorkerSupervisor._HAND_OVERLAY_MODULE,
            "insight_capture.media.hand_overlay_worker",
        )


if __name__ == "__main__":
    unittest.main()
