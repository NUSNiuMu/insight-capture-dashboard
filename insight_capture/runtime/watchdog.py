"""Report image and SDK pose input transitions independently."""

import threading

from insight_capture.runtime.diagnostics import input_snapshot


class ParticipantWatchdog:
    def __init__(self, owner):
        self.owner = owner
        self._closed = threading.Event()

    def close(self):
        self._closed.set()

    def check_inputs(self):
        if self.owner._playback_mode:
            return
        for name, evidence in input_snapshot(self.owner).items():
            image, vio = evidence["image_state"], evidence["vio_state"]
            self.owner.events.changed("image_input", name, image,
                level="info" if image == "receiving" else "warning", **evidence)
            self.owner.events.changed("vio_input", name, vio,
                level="info" if vio == "receiving" else "warning",
                age_sec=evidence["native_vio_age_sec"])

    def run(self):
        while not self._closed.wait(5.):
            self.check_inputs()
