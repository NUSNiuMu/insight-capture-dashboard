/* Signaling only; callers own rendering, connection lifetime and error display. */
export function openCameraWebRtc({ cameraName, port, fps, isCurrent, onTrack, onError }) {
  const url = new URL(location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  url.port = port;
  url.pathname = "/ws/webrtc";
  url.search = new URLSearchParams({ camera: cameraName, fps: String(fps) });
  const pc = new RTCPeerConnection();
  const ws = new WebSocket(url);
  let failed = false;
  const active = () => !failed && isCurrent();
  const fail = () => {
    if (!active()) return;
    failed = true;
    onError();
  };
  pc.ontrack = event => { if (active()) onTrack(event); };
  pc.onicecandidate = event => {
    if (active() && event.candidate && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "ice", candidate: event.candidate.candidate, sdpMLineIndex: event.candidate.sdpMLineIndex }));
    }
  };
  pc.onconnectionstatechange = () => {
    if (["failed", "closed"].includes(pc.connectionState)) fail();
  };
  // Process the offer before its following ICE messages, even across awaits.
  let signaling = Promise.resolve();
  ws.onmessage = event => {
    signaling = signaling.then(async () => {
      if (!active()) return;
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === "offer") {
        await pc.setRemoteDescription({ type: "offer", sdp: message.sdp });
        if (!active()) return;
        const answer = await pc.createAnswer();
        if (!active()) return;
        await pc.setLocalDescription(answer);
        if (!active()) return;
        ws.send(JSON.stringify({ type: "answer", sdp: pc.localDescription.sdp }));
      } else if (message.type === "ice" && message.candidate) {
        await pc.addIceCandidate({ candidate: message.candidate, sdpMLineIndex: message.sdpMLineIndex });
      } else if (message.type === "webrtc_unavailable") {
        fail();
      }
    }).catch(fail);
  };
  ws.onerror = ws.onclose = fail;
  return { pc, ws };
}
