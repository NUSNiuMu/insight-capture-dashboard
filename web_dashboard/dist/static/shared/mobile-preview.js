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
      const response = await fetch("/api/cameras", { cache: "no-store", signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error("设备状态读取失败");
      const camera = (await response.json()).cameras.find(c => c.name === name);
      if (!current()) return;
      if (!camera?.webrtc_available) throw new Error("该相机实时视频服务未就绪");
      const pc = this.pc = new RTCPeerConnection();
      const url = new URL(location.href);
      url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      url.port = camera.webrtc_port;
      url.pathname = "/ws/webrtc";
      url.search = new URLSearchParams({ camera: name, fps: "15" });
      const ws = this.ws = new WebSocket(url);
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
      pc.onicecandidate = e => {
        if (e.candidate && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ice", candidate: e.candidate.candidate, sdpMLineIndex: e.candidate.sdpMLineIndex }));
      };
      pc.ontrack = e => {
        if (!current()) return;
        this.video.srcObject = e.streams[0]; this.video.hidden = false;
        this.video.play().catch(() => fail("浏览器未能播放视频，请重新开启"));
      };
      ws.onmessage = async e => {
        if (!current()) return;
        try {
          const m = JSON.parse(e.data);
          if (m.type === "offer") {
            await pc.setRemoteDescription({ type: "offer", sdp: m.sdp });
            if (!current()) return;
            await pc.setLocalDescription(await pc.createAnswer());
            if (!current()) return;
            ws.send(JSON.stringify({ type: "answer", sdp: pc.localDescription.sdp }));
          } else if (m.type === "ice" && m.candidate) {
            await pc.addIceCandidate({ candidate: m.candidate, sdpMLineIndex: m.sdpMLineIndex });
          } else if (m.type === "webrtc_unavailable") fail("实时视频服务未就绪");
        } catch { fail(); }
      };
      ws.onerror = ws.onclose = () => fail();
      pc.onconnectionstatechange = () => { if (pc.connectionState === "failed") fail(); };
    } catch (error) { fail(error.message); }
  }
}
