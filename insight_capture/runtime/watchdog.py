"""DDS participant and camera-link watchdog."""

from __future__ import annotations

import fcntl
import os
import socket
import struct
import time
from typing import Optional


class ParticipantWatchdog:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _any_ros_data_received(self) -> bool:
        # Capture Mode intentionally leaves latest_camera_frames empty. Probe
        # the raw image-reader timestamps that exist before preview encoding.
        return (
            any(self.owner.camera_input_times.get(name) for name in self.owner.camera_input_times)
            or any(t > 0.0 for t in self.owner.last_pose_received_time.values())
            or any(
                t > 0.0
                for t in getattr(self.owner, "camera_liveness_times", {}).values()
            )
        )

    def _camera_last_seen(self, camera_name: str) -> float:
        input_times = self.owner.camera_input_times.get(camera_name) or ()
        image_seen = float(input_times[-1]) if input_times else 0.0
        pose_seen = float(
            getattr(self.owner, "last_pose_received_time", {}).get(camera_name, 0.0)
        )
        native_vio_seen = float(
            getattr(self.owner, "camera_liveness_times", {}).get(camera_name, 0.0)
        )
        return max(image_seen, pose_seen, native_vio_seen)

    @staticmethod
    def _camera_link_up() -> bool:
        # Camera USB-Ethernet links use link-local 169.254.x.x addresses.
        try:
            names = os.listdir("/sys/class/net")
        except OSError:
            return False
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for name in names:
                if name == "lo" or name.startswith("docker"):
                    continue
                try:
                    packed = fcntl.ioctl(
                        sock.fileno(),
                        0x8915,  # SIOCGIFADDR
                        struct.pack("256s", name.encode()[:15]),
                    )
                except OSError:
                    continue  # interface has no IPv4 address
                if socket.inet_ntoa(packed[20:24]).startswith("169.254."):
                    return True
        finally:
            sock.close()
        return False

    def _restart_for_stale_participant(self, reason: str) -> None:
        # Fast DDS needs a process restart to discover links added after startup.
        if os.path.exists("/.dockerenv"):
            self.owner.get_logger().error(
                f"{reason} -- exiting so the container restart policy recreates the DDS participant."
            )
            os._exit(1)
        self.owner.get_logger().warning(f"{reason} -- restart this process to recover.")

    def _recording_active(self) -> bool:
        manager = self.owner.recording_manager
        if manager is None:
            return False
        try:
            return manager.is_recording()
        except Exception:
            return False

    def _stale_participant_watchdog_loop(self) -> None:
        # Recover both boot-time link races and runtime camera-link drops.
        link_grace_sec = 60.0
        poll_sec = 5.0
        # Keep restart grace well above the UI stale threshold.
        camera_stall_grace_sec = 15.0
        link_up_since: Optional[float] = None
        while True:
            time.sleep(poll_sec)
            now = time.monotonic()

            if getattr(self.owner, "_playback_mode", False):
                # Prepared playback intentionally gates live camera callbacks.
                # Their stale timestamps must not be treated as a USB/DDS drop.
                link_up_since = None
                continue

            if not self.owner._any_ros_data_received():
                if not self.owner._camera_link_up():
                    link_up_since = None
                    continue
                if link_up_since is None:
                    link_up_since = now
                    continue
                if now - link_up_since < link_grace_sec:
                    continue
                self.owner._restart_for_stale_participant(
                    "Camera link up for 60s but no ROS data ever received -- DDS participant "
                    "likely predates the camera links"
                )
                link_up_since = now
                continue

            link_up_since = None

            if self.owner._recording_active():
                # Never interrupt an active recording for one stalled camera.
                continue

            if not self.owner._camera_link_up():
                # Avoid restart loops on camera-less playback machines.
                continue

            for camera in self.owner.cameras:
                last_seen = self._camera_last_seen(camera.name)
                if last_seen <= 0.0 or now - last_seen <= camera_stall_grace_sec:
                    continue
                self.owner._restart_for_stale_participant(
                    f"Camera '{camera.name}' produced no image, pose, or native VIO for over "
                    f"{camera_stall_grace_sec:.0f}s after previously streaming "
                    "(likely a USB/link drop)"
                )
                break
