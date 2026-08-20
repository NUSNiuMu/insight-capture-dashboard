import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "set_ros_domain_id.py"

SPEC = importlib.util.spec_from_file_location("set_ros_domain_id", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class SetRosDomainIdTests(unittest.TestCase):
    def test_domain_id_range(self) -> None:
        self.assertEqual(MODULE.validate_domain_id("0"), 0)
        self.assertEqual(MODULE.validate_domain_id("232"), 232)
        with self.assertRaises(Exception):
            MODULE.validate_domain_id("233")

    def test_parse_camera_urls_discovers_three_link_local_peers(self) -> None:
        output = """\
2: enx10    inet 169.254.10.2/24 brd 169.254.10.255 scope global enx10
3: enx20    inet 169.254.20.2/24 brd 169.254.20.255 scope global enx20
4: enx30    inet 169.254.30.2/24 brd 169.254.30.255 scope global enx30
5: docker0  inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0
6: wlan0    inet 192.168.19.222/24 brd 192.168.19.255 scope global wlan0
"""
        self.assertEqual(
            MODULE.parse_camera_urls(output),
            [
                "http://169.254.10.1",
                "http://169.254.20.1",
                "http://169.254.30.1",
            ],
        )

    def test_update_local_config_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config" / "cameras.json"
            env = root / ".env"
            config.parent.mkdir()
            config.write_text(
                json.dumps({"ros_domain_id": 20, "cameras": [{"name": "a"}]}),
                encoding="utf-8",
            )
            env.write_text("KEEP=value\nROS_DOMAIN_ID=20\n", encoding="utf-8")

            MODULE.update_camera_config(config, 21)
            MODULE.update_env_file(env, 21)

            self.assertEqual(json.loads(config.read_text())["ros_domain_id"], 21)
            self.assertEqual(env.read_text(), "KEEP=value\nROS_DOMAIN_ID=21\n")

    def test_env_update_appends_missing_value_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / ".env"
            env.write_text("KEEP=value\n", encoding="utf-8")
            MODULE.update_env_file(env, 22)
            MODULE.update_env_file(env, 23)
            self.assertEqual(env.read_text(), "KEEP=value\nROS_DOMAIN_ID=23\n")

    def test_no_restart_workflow_updates_all_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "cameras.json"
            env = root / ".env"
            config.write_text(
                json.dumps(
                    {
                        "ros_domain_id": 20,
                        "cameras": [
                            {"name": "a"},
                            {"name": "b"},
                            {"name": "head"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            urls = [
                "http://169.254.10.1",
                "http://169.254.20.1",
                "http://169.254.30.1",
            ]
            arguments = ["21", "--config", str(config), "--env-file", str(env)]
            for url in urls:
                arguments.extend(["--camera-url", url])
            arguments.extend(["--no-restart", "-y"])

            with (
                mock.patch.object(MODULE, "read_camera_domain", return_value=20),
                mock.patch.object(MODULE, "set_camera_domain") as update_camera,
                mock.patch.object(MODULE, "recording_is_active", return_value=False),
            ):
                self.assertEqual(MODULE.main(arguments), 0)

            self.assertEqual(update_camera.call_count, 3)
            self.assertEqual(json.loads(config.read_text())["ros_domain_id"], 21)
            self.assertEqual(env.read_text(), "ROS_DOMAIN_ID=21\n")

    def test_compose_services_read_domain_from_env(self) -> None:
        for path in (ROOT / "docker-compose.yml", ROOT / "deploy/docker-compose.yml"):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("ROS_DOMAIN_ID=20", content)
            self.assertIn("ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-20}", content)


if __name__ == "__main__":
    unittest.main()
