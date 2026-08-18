import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "install_voice_control_service.sh"
UPDATER = ROOT / "deploy" / "update.sh"


class DeployVoiceTest(unittest.TestCase):
    def test_deploy_shell_entrypoints_parse(self):
        subprocess.run(
            ["bash", "-n", str(INSTALLER), str(UPDATER)],
            check=True,
            cwd=ROOT,
        )

    def test_if_ready_skips_cleanly_without_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [str(INSTALLER), "--if-ready"],
                check=False,
                cwd=ROOT,
                env={**os.environ, "LOOPER_VOICE_ROOT": temporary},
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("assets are not provisioned", result.stderr)

    def test_upgrade_syncs_voice_runtime_and_migrates_legacy_unit(self):
        updater = UPDATER.read_text(encoding="utf-8")
        installer = INSTALLER.read_text(encoding="utf-8")

        self.assertIn("sync_host_voice_runtime", updater)
        self.assertIn('"${VOICE_INSTALLER}" --if-ready', updater)
        self.assertIn('legacy_unit="looper-openclaw-voice.service"', installer)
        self.assertIn("systemctl --user enable insight-voice-control.service", installer)


if __name__ == "__main__":
    unittest.main()
