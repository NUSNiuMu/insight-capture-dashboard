import { openCameraWebRtc } from "./webrtc.js?v=20261027-capture-cleanup";

/* The phone opens one WebRTC stream at a time. */
export class MobilePreview {
  constructor(video, label) {
    Object.assign(this, { video, label, generation: 0, pc: null, ws: null, timer: null });
  }
  stop() {
    this.generation++;
    clearTimeout(this.timer);
    this.pc?.close(); this.ws?.close();
    this.pc = this.ws = null;
    this.video.srcObject = null;
    this.video.hidden = true;
  }
  async start(name) {
    this.stop();
    const generation = this.generation;
    const current = () => generation === this.generation;
    const fail = (message = "Video disconnected. Reconnecting…") => {
      if (!current()) return;
      this.stop(); this.label.textContent = message;
      this.timer = setTimeout(() => this.start(name), 1500);
    };
    this.label.textContent = "Connecting video…";
    try {
      if (!window.RTCPeerConnection) throw new Error("This browser does not support live video.");
      let camera;
      const readyDeadline = performance.now() + 10000;
      while (current()) {
        const response = await fetch("/api/cameras", { cache: "no-store", signal: AbortSignal.timeout(5000) });
        if (!response.ok) throw new Error("Could not load device status.");
        camera = (await response.json()).cameras.find(c => c.name === name);
        if (!current()) return;
        if (!camera || camera.stale) throw new Error("No video input. Check the device connection.");
        if (camera.webrtc_available) break;
        if (performance.now() >= readyDeadline) throw new Error("Video startup timed out. Check the Jetson service.");
        this.label.textContent = "Starting video…";
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      if (!current()) return;
      this.label.textContent = "Connecting video…";
      const { pc, ws } = openCameraWebRtc({
        cameraName: name, port: camera.webrtc_port, fps: 15, isCurrent: current,
        onTrack: event => {
          this.video.srcObject = event.streams[0]; this.video.hidden = false;
          this.video.play().catch(() => fail("Could not play video. Reconnecting…"));
        },
        onError: () => fail(),
      });
      this.pc = pc; this.ws = ws;
      let progress = 0, lastFrame = performance.now();
      const watch = () => {
        if (!current()) return;
        if (this.video.currentTime > progress) {
          progress = this.video.currentTime; lastFrame = performance.now();
          this.label.textContent = "Live · up to 15 fps";
        }
        if (performance.now() - lastFrame > 10000) return fail("Video stopped. Reconnecting…");
        this.timer = setTimeout(watch, 1000);
      };
      watch();
    } catch (error) { fail(error.message); }
  }
}
