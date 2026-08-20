import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOST_SETUP = ROOT / "deploy" / "host_setup.sh"
NETWORK_SCRIPT = ROOT / "deploy" / "configure_camera_network.sh"
RPS_RULE = ROOT / "deploy" / "udev" / "99-insight-camera-rps.rules"


class CameraNetworkDeploymentTests(unittest.TestCase):
    def test_shell_scripts_parse(self) -> None:
        subprocess.run(
            ["bash", "-n", str(HOST_SETUP), str(NETWORK_SCRIPT)],
            check=True,
            cwd=ROOT,
        )

    def test_reconnect_rule_uses_udev_final_interface_name(self) -> None:
        rule = RPS_RULE.read_text(encoding="utf-8")

        self.assertIn('DRIVERS=="cdc_ncm"', rule)
        self.assertIn('ENV{ID_NET_NAME}=="enx*"', rule)
        self.assertIn('$env{ID_NET_NAME}', rule)
        self.assertNotIn('KERNEL=="enx*"', rule)

    def test_host_setup_installs_and_reloads_reconnect_rule(self) -> None:
        host_setup = HOST_SETUP.read_text(encoding="utf-8")

        self.assertIn("99-insight-camera-rps.rules", host_setup)
        self.assertIn("udevadm control --reload-rules", host_setup)


if __name__ == "__main__":
    unittest.main()
