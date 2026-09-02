import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DashboardLauncherStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temporary.name)
        (self.test_root / "deploy").mkdir()
        shutil.copy2(
            ROOT / "deploy" / "run_dashboard.sh",
            self.test_root / "deploy" / "run_dashboard.sh",
        )
        self.bin_dir = self.test_root / "bin"
        self.bin_dir.mkdir()
        self.capture_path = self.test_root / "compose-host-dir.txt"
        docker = self.bin_dir / "docker"
        docker.write_text(
            """#!/usr/bin/env bash
if [[ "$*" == "compose config --environment" ]]; then
    printf 'INSIGHT_ROSBAG_HOST_DIR=%s\n' "${TEST_CONFIGURED_SOURCE}"
    printf 'INSIGHT_ROSBAG_REQUIRED_SOURCE=%s\n' "${TEST_REQUIRED_SOURCE}"
    exit 0
fi
if [[ "$*" == "compose ps --status running --services" ]]; then
    exit 0
fi
if [[ "$*" == "compose up -d" ]]; then
    printf '%s\n' "${INSIGHT_ROSBAG_HOST_DIR:-${TEST_CONFIGURED_SOURCE}}" > "${TEST_CAPTURE_PATH}"
    exit 73
fi
exit 74
""",
            encoding="utf-8",
        )
        docker.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_launcher(self, configured_source: Path, required_source: Path):
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "TEST_CAPTURE_PATH": str(self.capture_path),
                "TEST_CONFIGURED_SOURCE": str(configured_source),
                "TEST_REQUIRED_SOURCE": str(required_source),
            }
        )
        return subprocess.run(
            [str(self.test_root / "deploy" / "run_dashboard.sh")],
            cwd=self.test_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_missing_required_device_uses_stable_fallback_bind(self) -> None:
        configured = self.test_root / "unavailable-automount" / "rosbags"
        required = self.test_root / "missing-device"

        result = self.run_launcher(configured, required)

        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(
            self.capture_path.read_text(encoding="utf-8").strip(),
            str(self.test_root / "rosbags"),
        )
        self.assertIn("starting with NVMe fallback", result.stdout)
        self.assertFalse(configured.exists())

    def test_available_required_device_keeps_usb_bind(self) -> None:
        configured = self.test_root / "usb" / "rosbags"
        configured.mkdir(parents=True)
        required = self.test_root / "device"
        required.touch()
        findmnt = self.bin_dir / "findmnt"
        findmnt.write_text(
            "#!/usr/bin/env bash\nprintf '%s[/rosbags]\\n' \"${TEST_REQUIRED_SOURCE}\"\n",
            encoding="utf-8",
        )
        findmnt.chmod(0o755)

        result = self.run_launcher(configured, required)

        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(
            self.capture_path.read_text(encoding="utf-8").strip(),
            str(configured),
        )
        self.assertNotIn("NVMe fallback", result.stdout)


if __name__ == "__main__":
    unittest.main()
