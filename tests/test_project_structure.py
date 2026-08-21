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

    def test_compatibility_imports_resolve_to_canonical_objects(self) -> None:
        from insight_capture.api import WebDashboardServer
        from insight_capture.core.models import PoseSample
        from insight_capture.perception.gripper import GripperMarkerDetector
        from insight_capture.services import UmiExportManager
        from insight_capture.common.models import PoseSample as LegacyPoseSample
        from insight_capture.postprocess.gripper import (
            GripperMarkerDetector as LegacyGripperMarkerDetector,
        )
        from insight_capture.web import WebDashboardServer as LegacyWebDashboardServer
        from insight_capture.web.umi_export import (
            UmiExportManager as LegacyUmiExportManager,
        )

        self.assertIs(LegacyPoseSample, PoseSample)
        self.assertIs(LegacyGripperMarkerDetector, GripperMarkerDetector)
        self.assertIs(LegacyWebDashboardServer, WebDashboardServer)
        self.assertIs(LegacyUmiExportManager, UmiExportManager)

    def test_production_code_uses_canonical_package_boundaries(self) -> None:
        compatibility_files = {
            ROOT / "insight_capture" / "postprocess" / "gripper" / "calibration.py",
            ROOT / "insight_capture" / "postprocess" / "gripper" / "overlay.py",
            ROOT / "insight_capture" / "postprocess" / "gripper" / "tracking.py",
            ROOT / "insight_capture" / "postprocess" / "quality" / "station_check.py",
        }
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
            if (
                path in compatibility_files
                or package_root / "common" in path.parents
                or package_root / "web" in path.parents
            ):
                continue
            source = path.read_text(encoding="utf-8")
            for forbidden in forbidden_imports:
                if forbidden in source:
                    violations.append(f"{path.relative_to(ROOT)}: {forbidden}")
        self.assertEqual(violations, [])

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

    def test_frontend_source_and_task_labels_are_english_only(self) -> None:
        han = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
        violations = []
        frontend_root = ROOT / "web_dashboard" / "src"
        for path in frontend_root.rglob("*"):
            if path.suffix not in {".css", ".html", ".js"}:
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if han.search(line):
                    violations.append(f"{path.relative_to(ROOT)}:{line_number}")

        task_config = json.loads(
            (ROOT / "config" / "capture_tasks.json").read_text(encoding="utf-8")
        )
        for task in task_config.get("tasks", []):
            if han.search(str(task.get("name") or "")):
                violations.append(f"config/capture_tasks.json:{task.get('id')}:name")

        self.assertEqual(violations, [])

    def test_dashboard_navigation_order_is_consistent(self) -> None:
        expected = [
            "/",
            "/3d",
            "/bags",
            "/umi-dataset",
            "/scoring",
            "/handpose",
            "/optimization",
            "/recording",
            "/settings",
        ]
        frontend_root = ROOT / "web_dashboard" / "src"
        checked = []
        for path in frontend_root.rglob("*.html"):
            source = path.read_text(encoding="utf-8")
            navigation = re.search(
                r'<nav class="(?:side-nav|rail-nav)"[^>]*>(.*?)</nav>',
                source,
                flags=re.DOTALL,
            )
            if navigation is None:
                continue
            links = re.findall(r'<a href="([^"]+)"', navigation.group(1))
            self.assertEqual(links, expected, str(path.relative_to(ROOT)))
            checked.append(path)
        self.assertEqual(len(checked), 9)

    def test_dashboard_launcher_reconciles_compose_before_restart(self) -> None:
        source = (ROOT / "deploy" / "run_dashboard.sh").read_text(encoding="utf-8")
        self.assertIn("docker compose up -d", source)
        self.assertNotIn("docker compose restart\n", source)

    def test_dashboard_launcher_preserves_raw_only_calibration_capture(self) -> None:
        source = (ROOT / "deploy" / "run_dashboard.sh").read_text(encoding="utf-8")
        wait_loop = source[source.index("while true; do") :]
        self.assertIn("native_vio_fresh", source)
        self.assertIn("if recording_is_active; then", wait_loop)
        self.assertLess(
            wait_loop.index("if recording_is_active; then"),
            wait_loop.index("docker compose restart insight-dashboard"),
        )


if __name__ == "__main__":
    unittest.main()
