#!/usr/bin/env python3
"""Handle the Jetson carrier power key and preserve the current recording."""

import fcntl
import json
import logging
import math
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

INPUT_DEVICE = Path("/dev/input/by-path/platform-gpio-keys-event")
EVENT = struct.Struct("@llHHi")
EV_KEY = 1
KEY_POWER = 116
EVIOCGRAB = 0x40044590
EVIOCGKEY = 0x80104518  # Read the first 128 current key states.
HOLD_SECONDS = 2.0
SAVE_TIMEOUT = 300
RECORDING_URL = f"http://127.0.0.1:{os.environ.get('INSIGHT_CAPTURE_PORT', '8765')}/api/recording"
BUSY_STATES = {"starting", "preparing", "stopping", "saving", "finalizing", "copying", "merging", "stop_pending"}


def recording_request(endpoint, payload=None):
    request = Request(f"{RECORDING_URL}/{endpoint}")
    timeout = 4
    if payload is not None:
        request = Request(
            request.full_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        timeout = 45
    with urlopen(request, timeout=timeout) as response:
        status = json.load(response)
    if not isinstance(status, dict) or not isinstance(status.get("recording"), bool):
        raise RuntimeError("Invalid recording status")
    return status


def dashboard_is_stopped():
    result = subprocess.run(
        ["/usr/bin/docker", "ps", "--quiet", "--filter", "name=^/insight-dashboard$"],
        capture_output=True, text=True, timeout=4,
    )
    if result.returncode != 0 or result.stdout.strip():
        return False
    service = subprocess.run(
        ["/usr/bin/systemctl", "show", "-p", "ActiveState", "--value", "insight-capture.service"],
        capture_output=True, text=True, timeout=4,
    )
    return service.stdout.strip() in {"inactive", "failed"}


def wait_for_capture(deadline):
    saw_activity = False
    stop_requested = False
    request_uncertain_until = 0
    last_status = None
    while time.monotonic() < deadline:
        try:
            status = recording_request("status")
        except (URLError, TimeoutError) as exc:
            if (
                not saw_activity
                and isinstance(exc, URLError)
                and isinstance(exc.reason, ConnectionRefusedError)
                and dashboard_is_stopped()
            ):
                logging.info("Dashboard is stopped; no active capture to finalize")
                return
            raise RuntimeError("Recording status unavailable") from exc
        if status.get("upgrade_lock_supported") is not True:
            raise RuntimeError("Recording start lock unavailable")

        take = status.get("current_take") or {}
        distributed = status.get("distributed") or {}
        capture_state = status.get("state")
        merge_state = status.get("merge_state")
        phase = distributed.get("phase")
        take_state = take.get("state")
        if merge_state == "error" or phase == "error" or take_state == "finalization_failed":
            raise RuntimeError(status.get("merge_error") or "Recording finalization failed")

        busy = status.get("operation_in_progress") or any(
            state in BUSY_STATES for state in (capture_state, merge_state, phase, take_state)
        )
        if not status["recording"] and not busy and take_state != "recording":
            if phase == "saved" and not (distributed.get("local_saved") and distributed.get("remote_saved")):
                raise RuntimeError("Both recordings must be saved")
            logging.info("Capture safe for power-off (merge=%s, take=%s, distributed=%s)",
                         merge_state, take_state, phase)
            return

        saw_activity = True
        state = (status["recording"], bool(busy), merge_state, take_state, phase)
        if state != last_status:
            logging.info("Waiting for capture (recording=%s, busy=%s, merge=%s, take=%s, distributed=%s)", *state)
            last_status = state

        if status["recording"] and not busy and not stop_requested and time.monotonic() >= request_uncertain_until:
            payload = {key: status.get(key) for key in ("output_path", "recording_mode")}
            if not all(isinstance(value, str) and value for value in payload.values()):
                payload = {}
            try:
                result = recording_request("stop", payload)
            except HTTPError as exc:
                if exc.code != 409:
                    raise
                request_uncertain_until = time.monotonic() + 1
            except (URLError, TimeoutError):
                # Stop may have been accepted before the connection timed out.
                request_uncertain_until = time.monotonic() + 3
            else:
                if result.get("merge_state") == "error":
                    raise RuntimeError(result.get("merge_error") or "Recording stop failed")
                stop_requested = True
                logging.info("Dashboard accepted normal recording stop")
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for recording to save")


def request_poweroff(lock_path):
    try:
        deadline = time.monotonic() + SAVE_TIMEOUT
        with open(lock_path, "rb", opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW)) as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Recording start or deployment lock is busy")
                    time.sleep(0.5)
            wait_for_capture(deadline)
            logging.info("Requesting normal system power-off")
            subprocess.run(["/usr/bin/systemctl", "--no-block", "poweroff"], check=True, timeout=10)
            # Prevent a new Take from starting while systemd is stopping services.
            time.sleep(120)
            logging.error("System did not power off after 120 seconds")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        logging.error("Power-off cancelled: %s", exc)


def key_is_down(device):
    keys = bytearray(16)
    fcntl.ioctl(device, EVIOCGKEY, keys, True)
    return bool(keys[KEY_POWER // 8] & (1 << (KEY_POWER % 8)))


def watch_button(device, poweroff):
    poll = select.poll()
    poll.register(device, select.POLLIN)
    deadline = None
    while True:
        timeout = None if deadline is None else max(0, math.ceil((deadline - time.monotonic()) * 1000))
        for _, flags in poll.poll(timeout):
            if flags & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                raise OSError("Power button input disconnected")
            while True:
                try:
                    data = os.read(device, EVENT.size * 32)
                except BlockingIOError:
                    break
                if not data:
                    raise OSError("Power button input closed")
                for _, _, kind, code, value in EVENT.iter_unpack(data):
                    if kind == 0 and code == 3:  # SYN_DROPPED: disregard any in-flight hold.
                        deadline = None
                    elif kind == EV_KEY and code == KEY_POWER:
                        if value == 1:
                            deadline = time.monotonic() + HOLD_SECONDS
                        elif value == 0 and deadline is not None:
                            deadline = None
                            logging.info("Short power-key press ignored")
        if deadline is not None and time.monotonic() >= deadline:
            deadline = None
            if key_is_down(device):
                logging.info("Power key held for %.1f seconds", HOLD_SECONDS)
                poweroff()


def notify_ready():
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
        notification.sendto(b"READY=1", address)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("/var/log/insight-power-button/events.log"),
        ],
    )
    lock_path = Path(sys.argv[1])
    event_name = INPUT_DEVICE.resolve(strict=True).name
    device_name = Path(f"/sys/class/input/{event_name}/device/name").read_text().strip()
    if device_name != "gpio-keys":
        raise RuntimeError(f"Unexpected input device: {device_name}")
    with open(INPUT_DEVICE, "rb", opener=lambda path, flags: os.open(path, flags | os.O_NONBLOCK)) as device:
        fcntl.ioctl(device, EVIOCGRAB, 1)
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        logging.info("Watching %s for a %.1f-second hold (boot %s)", INPUT_DEVICE, HOLD_SECONDS, boot_id)
        notify_ready()
        watch_button(device.fileno(), lambda: request_poweroff(lock_path))


if __name__ == "__main__":
    main()
