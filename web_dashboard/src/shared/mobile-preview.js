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
    const fail = (message = "预览连接已断开，请关闭后重新开启") => {
      if (!current()) return;
      this.stop(); this.label.textContent = message;
    };
    this.label.textContent = "正在连接视频…";
    try {
      if (!window.RTCPeerConnection) throw new Error("此浏览器不支持实时视频");
      let camera;
      const readyDeadline = performance.now() + 10000;
      while (current()) {
        const response = await fetch("/api/cameras", { cache: "no-store", signal: AbortSignal.timeout(5000) });
        if (!response.ok) throw new Error("设备状态读取失败");
        camera = (await response.json()).cameras.find(c => c.name === name);
        if (!current()) return;
        if (!camera || camera.stale) throw new Error("该相机当前无图像输入，请检查设备连接");
        if (camera.webrtc_available) break;
        if (performance.now() >= readyDeadline) throw new Error("视频服务启动超时，请检查 Jetson 服务状态");
        this.label.textContent = "正在启动视频服务…";
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      if (!current()) return;
      this.label.textContent = "正在连接视频…";
      const { pc, ws } = openCameraWebRtc({
        cameraName: name, port: camera.webrtc_port, fps: 15, isCurrent: current,
        onTrack: event => {
          this.video.srcObject = event.streams[0]; this.video.hidden = false;
          this.video.play().catch(() => fail("浏览器未能播放视频，请重新开启"));
        },
        onError: () => fail(),
      });
      this.pc = pc; this.ws = ws;
      let progress = 0, lastFrame = performance.now();
      const watch = () => {
        if (!current()) return;
        if (this.video.currentTime > progress) {
          progress = this.video.currentTime; lastFrame = performance.now();
          this.label.textContent = "实时视频 · 最高 15 fps";
        }
        if (performance.now() - lastFrame > 10000) return fail("画面已停更，请关闭后重新开启");
        this.timer = setTimeout(watch, 1000);
      };
      watch();
    } catch (error) { fail(error.message); }
  }
}
