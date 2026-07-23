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
        # GIL-atomic dict reads; values only ever go None->frame / 0.0->t
        # before the first user-triggered reset, so no locks needed for a
        # boolean "has anything ever arrived".
        return any(frame is not None for frame in self.owner.latest_camera_frames.values()) or any(
            t > 0.0 for t in self.owner.last_pose_received_time.values()
        )

    @staticmethod
    def _camera_link_up() -> bool:
        # A 169.254.x.x address on any non-loopback/docker interface is a
        # camera's point-to-point USB-ethernet link (see
        # scripts/reboot_cameras.sh) -- i.e. a camera is physically
        # connected, whether or not its ROS stack is publishing yet. Pure
        # ioctls (microseconds) rather than shelling out to `ip`, so the
        # watchdog poll is effectively free.
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
        # Shared exit path for both watchdog cases below. Fast DDS enumerates
        # network interfaces only at participant creation, so a link that
        # appeared/changed after this process started stays invisible to it
        # until the participant is recreated, i.e. until this process
        # restarts. Inside docker, `restart: unless-stopped` (docker-compose.yml)
        # brings it back with the current links present; outside docker there
        # is no such policy, so warn instead of exiting into nothing.
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
        # Fast DDS enumerates network interfaces only at participant
        # creation, so two related failure modes share the same fix
        # (recreate the participant, i.e. restart this process):
        #
        # 1. Boot race: this container auto-starts at host boot (restart:
        #    unless-stopped), usually before the per-camera USB-ethernet
        #    links exist, so the participant advertises unicast locators the
        #    cameras can't route to and never receives a single message.
        #    That state does NOT self-heal (observed fully stale >15min
        #    while a fresh `ros2 topic list` in the same container saw
        #    every topic instantly). run_dashboard.sh has the same
        #    link-presence check, but only runs once when someone invokes
        #    the script -- this covers headless boots too.
        # 2. Runtime drop: a camera that WAS streaming loses its link (USB
        #    unplugged and replugged) and never comes back on its own, for
        #    the same interface-enumeration reason. Case 1's "any data ever
        #    received" check can't see this -- the other cameras are still
        #    flowing -- so this needs its own per-camera staleness check.
        #
        # This thread never retires: case 1 can only ever fire once per
        # process (after that, "some data has arrived" is permanent), but
        # case 2 needs to keep watching for the life of the process.
        link_grace_sec = 60.0
        poll_sec = 5.0
        # Generous vs. camera_stale_timeout_sec (the UI's "no signal" flag,
        # default 2s) so a brief frame gap or exposure hiccup can't trigger a
        # restart -- this only fires on a camera that stays silent.
        camera_stall_grace_sec = 15.0
        link_up_since: Optional[float] = None
        while True:
            time.sleep(poll_sec)
            now = time.monotonic()

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
                # Don't kill an in-progress recording over one stalled
                # camera -- the others are still capturing fine, and a
                # full-process restart (the only fix DDS gives us) would cut
                # all of them short over one dropout.
                continue

            if not self.owner._camera_link_up():
                # No camera USB-ethernet link exists, so "frames stopped" is
                # not a recoverable link drop and a restart fixes nothing.
                # Concretely: on a machine with no cameras attached, bag
                # playback populates camera_frame_times, and when the bag
                # ends the stall check below would restart-loop the backend
                # (observed 2026-07-12 on the dev machine after the fleet
                # moved to another device).
                continue

            for camera in self.owner.cameras:
                frame_times = self.owner.camera_frame_times[camera.name]
                if not frame_times or now - frame_times[-1] <= camera_stall_grace_sec:
                    continue
                self.owner._restart_for_stale_participant(
                    f"Camera '{camera.name}' produced no frames for over "
                    f"{camera_stall_grace_sec:.0f}s after previously streaming "
                    "(likely a USB/link drop)"
                )
                break
