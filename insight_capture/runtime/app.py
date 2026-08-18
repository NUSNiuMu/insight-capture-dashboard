#!/usr/bin/env python3
"""Compose and run the field-capture runtime."""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import threading
import time
import traceback

try:
    import rclpy
    from rclpy.executors import MultiThreadedExecutor
except Exception:  # pragma: no cover - help and import work outside ROS
    rclpy = None
    MultiThreadedExecutor = None

from insight_capture.api import WebDashboardServer
from insight_capture.api.context import DashboardContext
from insight_capture.core.paths import PROJECT_ROOT, runtime_config_path
from insight_capture.runtime.bootstrap import (
    RuntimeSettings,
    build_recording_manager,
    configure_crash_diagnostics,
    load_runtime_settings,
)
from insight_capture.composition import build_runtime_services
from insight_capture.runtime.ros import PoseBridgeNode, make_image_qos, make_qos

__all__ = [
    "PoseBridgeNode",
    "default_runtime_config_path",
    "main",
    "make_image_qos",
    "make_qos",
    "parse_args",
]


def default_runtime_config_path() -> Path:
    """Return the canonical runtime config with legacy fallback support."""

    return runtime_config_path()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "cameras.json"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--webrtc-port", type=int, default=8766)
    parser.add_argument(
        "--web-root",
        default=str(PROJECT_ROOT / "web_dashboard" / "dist"),
    )
    parser.add_argument("--view-mode", choices=("3d",), default="3d")
    parser.add_argument("--fake-pose", action="store_true")
    parser.add_argument("--pose-publish-hz", type=float, default=50.0)
    parser.add_argument(
        "--runtime-config",
        "--post-processing-config",
        dest="post_processing_config",
        default=str(default_runtime_config_path()),
    )
    parser.add_argument("--rosbag-dir", "-rosbag-dir", default=None)
    return parser.parse_args()


def _run_executor(executor: "MultiThreadedExecutor", node: PoseBridgeNode) -> None:
    """Exit on executor failure so Docker can recover the ROS process."""

    try:
        executor.spin()
    except Exception:
        node.get_logger().fatal(
            "ROS executor thread crashed; exiting so the container restarts.\n"
            + traceback.format_exc()
        )
        os._exit(1)


def _warn_for_storage_fallback(
    node: PoseBridgeNode,
    settings: RuntimeSettings,
) -> None:
    if not settings.storage_status["using_fallback"]:
        return
    storage_failure = (
        settings.storage_status.get("fallback_reason")
        or settings.storage_status["mounted_source"]
        or "not mounted"
    )
    node.get_logger().warning(
        "Configured recording drive is unavailable "
        f"({storage_failure}); "
        f"falling back to NVMe at {settings.rosbag_root}"
    )


def main() -> None:
    args = parse_args()
    crash_log = configure_crash_diagnostics()
    settings = load_runtime_settings(
        config_path=Path(args.config),
        runtime_config_path=Path(args.post_processing_config),
        rosbag_dir=args.rosbag_dir,
    )
    if rclpy is None or MultiThreadedExecutor is None:
        raise RuntimeError("rclpy is not available in this environment")

    os.environ.setdefault("ROS_DOMAIN_ID", str(settings.ros_domain_id))
    rclpy.init(args=None)
    node = PoseBridgeNode(
        settings.config_path,
        post_processing_config_path=settings.runtime_config_path,
        fake_pose=args.fake_pose,
        pose_publish_hz=args.pose_publish_hz,
        webrtc_port=args.webrtc_port,
    )
    node.post_processing_config = settings.runtime_config
    node.get_logger().info(f"View mode={args.view_mode}")
    _warn_for_storage_fallback(node, settings)

    recording_manager = build_recording_manager(settings, node)
    recording_manager.start_orphan_recovery()
    node.recording_manager = recording_manager
    node.configure_capture_check(
        dict(settings.runtime_config.get("capture_check") or {}),
        settings.results_root,
    )

    executor = MultiThreadedExecutor()
    executor.add_node(node)
    spin_thread = threading.Thread(
        target=_run_executor,
        args=(executor, node),
        daemon=True,
        name="ros_executor",
    )
    spin_thread.start()

    web_root = Path(args.web_root) if args.web_root else None
    services = build_runtime_services(
        node=node,
        project_root=node.project_root,
        recording_manager=recording_manager,
        results_root=settings.results_root,
        runtime_config=settings.runtime_config,
    )
    context = DashboardContext(
        node=node,
        web_root=web_root.resolve() if web_root else None,
        project_root=node.project_root.resolve(),
        recording_manager=recording_manager,
        results_root=settings.results_root.resolve(),
        **services.dashboard_dependencies(),
    )
    server = WebDashboardServer(context, args.host, args.port)
    server.start()

    try:
        while rclpy.ok():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        services.active_qc.stop()
        executor.shutdown()
        node.close()
        node.destroy_node()
        with contextlib.suppress(Exception):
            rclpy.shutdown()
        spin_thread.join(timeout=1.0)
        crash_log.close()


if __name__ == "__main__":
    main()
