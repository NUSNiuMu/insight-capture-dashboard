import json
from pathlib import Path
import re
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
            "set_ros_domain_id.py",
            "merge_distributed_recording.py",
            "sync_camera_restart.py",
            "system_doctor.py",
            "system_doctor.sh",
        }
        actual = {path.name for path in (ROOT / "scripts").iterdir() if path.is_file()}
        self.assertEqual(actual, expected)

    def test_application_services_use_package_modules(self) -> None:
        from insight_capture.media.worker_supervisor import WorkerSupervisor
        from insight_capture.services.dataset_export import UmiExportManager
        from insight_capture.services.gripper_extraction import GripperExtractionManager
        from insight_capture.services.scoring import ScoringManager

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

    def test_production_code_uses_canonical_package_boundaries(self) -> None:
        forbidden_imports = (
            "insight_capture.common",
            "insight_capture.web",
            "insight_capture.postprocess.gripper.calibration",
            "insight_capture.postprocess.gripper.overlay",
            "insight_capture.postprocess.gripper.tracking",
            "insight_capture.postprocess.quality.station_check",
        )
        violations = []
        package_root = ROOT / "insight_capture"
        for path in package_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in forbidden_imports:
                if forbidden in source:
                    violations.append(f"{path.relative_to(ROOT)}: {forbidden}")
        self.assertEqual(violations, [])

    def test_retired_namespaces_are_absent(self) -> None:
        for name in ("common", "web"):
            self.assertFalse((ROOT / "insight_capture" / name).exists())

    def test_runtime_modules_respect_delivery_and_postprocess_boundaries(self) -> None:
        violations = []
        runtime_root = ROOT / "insight_capture" / "runtime"
        composition_root = runtime_root / "app.py"
        forbidden_imports = ("insight_capture.api", "insight_capture.postprocess")
        for path in runtime_root.rglob("*.py"):
            if path == composition_root:
                continue
            source = path.read_text(encoding="utf-8")
            for forbidden in forbidden_imports:
                if forbidden in source:
                    violations.append(f"{path.relative_to(ROOT)}: {forbidden}")
        self.assertEqual(violations, [])

    def test_dashboard_context_only_stores_injected_dependencies(self) -> None:
        from insight_capture.api.context import DashboardContext

        dependency = object()
        context = DashboardContext(
            node=dependency,
            web_root=None,
            project_root=ROOT,
            recording_manager=dependency,
            bag_library=dependency,
            results_root=ROOT / "outputs",
            scoring_manager=dependency,
            prepared_playback_manager=dependency,
            optimization_manager=dependency,
            handpose_manager=dependency,
            gripper_extraction_manager=dependency,
            umi_export_manager=dependency,
            take_store=dependency,
            capture_preflight=dependency,
            voice_alerts=dependency,
            active_qc=dependency,
            playback_configuration=lambda: ([], []),
        )

        self.assertIs(context.active_qc, dependency)
        self.assertIs(context.scoring_manager, dependency)

    def test_runtime_package_does_not_reexport_other_layers(self) -> None:
        source = (ROOT / "insight_capture" / "runtime" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("from insight_capture.", source)

    def test_runtime_app_is_wiring_only(self) -> None:
        import ast

        path = ROOT / "insight_capture" / "runtime" / "app.py"
        source = path.read_text(encoding="utf-8")
        module = ast.parse(source)
        self.assertFalse(
            [node.name for node in module.body if isinstance(node, ast.ClassDef)]
        )
        self.assertLessEqual(len(source.splitlines()), 200)

    def test_phone_page_has_no_retired_desktop_links(self) -> None:
        source = (ROOT / "web_dashboard" / "src" / "mobile" / "index.html").read_text()
        links = re.findall(r'href="([^"#]+)"', source)
        self.assertTrue(all(link.startswith("/static/") for link in links), links)
        self.assertIn('id="task-create"', source)
        self.assertIn('id="saved-recordings"', source)

    def test_dashboard_launcher_reconciles_compose_before_restart(self) -> None:
        source = (ROOT / "deploy" / "run_dashboard.sh").read_text(encoding="utf-8")
        self.assertLess(source.index("    start_device_services"),
                        source.index("docker compose restart insight-dashboard"))
        self.assertNotIn("docker compose restart\n", source)

    def test_dashboard_launcher_preserves_active_capture(self) -> None:
        source = (ROOT / "deploy" / "run_dashboard.sh").read_text(encoding="utf-8")
        self.assertLess(
            source.index('if [[ "${capture_busy}" == true ]]; then'),
            source.index("docker compose restart insight-dashboard"),
        )


if __name__ == "__main__":
    unittest.main()
