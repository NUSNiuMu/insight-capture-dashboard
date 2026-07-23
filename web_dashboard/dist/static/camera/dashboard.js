import { escapeHtml } from "../shared/format.js";

const cameraDock = document.getElementById("camera-dock");
const cameraPageMeta = document.getElementById("camera-page-meta");
const enableCameras = Boolean(cameraDock);
const CAMERA_FPS_WINDOW_MS = 1500;
const CAMERA_POLL_INTERVAL_MS = 50;
const WEBRTC_RETRY_DELAY_MS = 5000;
const WEBRTC_MAX_ATTEMPTS = 5;
const WEBRTC_FIRST_FRAME_TIMEOUT_MS = 8000;
const cameraPanels = new Map();
const cameraPollState = new Map();
const cameraWebRtc = new Map();
let maximizedCameraName = null;
let pageUnloading = false;

export function startCameraDashboard() {
  startCameraPolling();
}

function startCameraPolling() {
  if (!enableCameras || !cameraDock) {
    return;
  }
  pollCameraMetadata();
  window.setInterval(pollCameraMetadata, CAMERA_POLL_INTERVAL_MS);
  // Catch up immediately when the tab becomes visible again (the poll below
  // no-ops while hidden, so without this the first update after returning
  // could lag by up to one interval).
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      pollCameraMetadata();
    }
  });
}

async function pollCameraMetadata() {
  // A hidden tab keeps its timers (throttled to ~1Hz by the browser) but
  // nobody can see the panels -- skip the fetch/DOM work entirely and let
  // the visibilitychange handler refresh the moment the tab returns.
  if (document.hidden) {
    return;
  }
  try {
    const response = await fetch(`/api/cameras?ts=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    if (payload.type !== "camera_update") {
      return;
    }
    const isPlayback = Boolean(payload.playback_mode);
    renderCameraPanels(payload.cameras || [], isPlayback);
    if (cameraPageMeta) {
      const liveCount = (payload.cameras || []).filter((camera) => !camera.stale && camera.visible).length;
      cameraPageMeta.textContent = `${liveCount}/${(payload.cameras || []).length} streams ${isPlayback ? "playback" : "live"}`;
    }
  } catch (_error) {
    // The pose WebSocket remains the primary status signal; image polling can retry quietly.
  }
}


function renderCameraPanels(cameras, isPlayback = false) {
  if (!cameraDock) {
    return;
  }
  const seen = new Set();
  cameras
    .slice()
    .sort((a, b) => {
      const orderA = Number(a.column || 0) * 100 + Number(a.row || 0);
      const orderB = Number(b.column || 0) * 100 + Number(b.row || 0);
      return orderA - orderB;
    })
    .forEach((camera, index) => {
    seen.add(camera.name);
    const panel = ensureCameraPanel(camera);
    panel.classList.toggle("is-stale", Boolean(camera.stale));
    updateCameraPanelAspect(panel, camera);
    updateCameraPanelLayout(panel, index);
    const status = panel.querySelector("[data-camera-status]");
    status.textContent = camera.stale ? "stale" : camera.visible ? (isPlayback ? "playback" : "live") : "waiting";
    const topic = panel.querySelector("[data-camera-topic]");
    if (topic && camera.topic) {
      topic.textContent = camera.topic;
    }
    updateCameraStream(panel, camera);
    updateCameraFps(camera.name, Number(camera.fps || 0));
    maybeStartCameraWebRtc(camera, panel);
    });
  for (const [name, panel] of cameraPanels.entries()) {
    if (!seen.has(name)) {
      stopCameraWebRtc(name);
      panel.remove();
      cameraPanels.delete(name);
      cameraPollState.delete(name);
    }
  }
}

function ensureCameraPanel(camera) {
  if (cameraPanels.has(camera.name)) {
    return cameraPanels.get(camera.name);
  }
  const panel = document.createElement("section");
  panel.className = "camera-panel";
  panel.dataset.cameraName = camera.name;
  panel.innerHTML = `
    <div class="camera-header">
      <div class="camera-title">
        <strong>${escapeHtml(camera.label || camera.name)}</strong>
        <span data-camera-topic>${escapeHtml(camera.topic || camera.name)}</span>
      </div>
      <div class="camera-actions">
        <button type="button" data-camera-maximize title="Maximize">□</button>
        <button type="button" data-camera-toggle title="Minimize">−</button>
      </div>
    </div>
    <div class="camera-body">
      <img class="camera-frame" alt="${escapeHtml(camera.label || camera.name)}">
      <video class="camera-frame camera-video" autoplay muted playsinline style="display:none"></video>
      <div class="camera-overlay">
        <span class="camera-fps" data-camera-fps>-- fps</span>
        <span data-camera-status>waiting</span>
      </div>
    </div>
  `;
  const img = panel.querySelector("img.camera-frame");
  img.addEventListener("load", () => {
    recordDisplayedFrame(camera.name);
  });
  const toggle = panel.querySelector("[data-camera-toggle]");
  toggle.addEventListener("click", () => {
    const minimized = panel.classList.toggle("minimized");
    toggle.textContent = minimized ? "+" : "−";
    toggle.title = minimized ? "Restore" : "Minimize";
  });
  const maximize = panel.querySelector("[data-camera-maximize]");
  maximize.addEventListener("click", () => {
    toggleCameraMaximized(camera.name);
  });
  const body = panel.querySelector(".camera-body");
  body.addEventListener("dblclick", () => toggleCameraMaximized(camera.name));
  panel.querySelector(".camera-header").addEventListener("dblclick", () => toggleCameraMaximized(camera.name));
  cameraDock.appendChild(panel);
  cameraPanels.set(camera.name, panel);
  cameraPollState.set(camera.name, {
    frameUrl: "",
    version: -1,
    aspectInitialized: false,
    backendFps: 0,
    displayFrameTimes: []
  });
  return panel;
}

function updateCameraPanelAspect(panel, camera) {
  const body = panel.querySelector(".camera-body");
  const rotation = normalizeRotation(camera.rotation_deg || 0);
  body.style.setProperty("--camera-rotation", `${rotation}deg`);
  if (camera.width && camera.height) {
    const rotated = rotation === 90 || rotation === 270;
    const aspectWidth = rotated ? camera.height : camera.width;
    const aspectHeight = rotated ? camera.width : camera.height;
    body.style.setProperty("--camera-aspect", `${aspectWidth} / ${aspectHeight}`);
    body.dataset.hasFrame = "true";
  } else {
    body.style.setProperty("--camera-aspect", "16 / 9");
    body.dataset.hasFrame = "false";
  }
}

function updateCameraPanelLayout(panel, index) {
  if (cameraDock?.classList.contains("spatial-camera-dock")) {
    panel.style.gridColumn = "1 / span 1";
    panel.style.gridRow = `${index + 1} / span 1`;
    return;
  }
  panel.style.gridColumn = `${index + 1} / span 1`;
  panel.style.gridRow = "1 / span 1";
}

function updateCameraStream(panel, camera) {
  const rtcState = cameraWebRtc.get(camera.name);
  if (rtcState && rtcState.active) {
    // Frames arrive over the WebRTC <video>; skip the per-frame HTTP GET
    // entirely. If the connection drops, scheduleWebRtcRetry() clears
    // `active` and this path resumes on the next poll tick.
    return;
  }
  const img = panel.querySelector("img.camera-frame");
  const pollState = cameraPollState.get(camera.name) || { frameUrl: "", version: -1 };
  const version = Number(camera.version || 0);
  if (
    pollState.frameUrl === camera.frame_url &&
    pollState.version === version &&
    img.getAttribute("src")
  ) {
    return;
  }
  pollState.frameUrl = camera.frame_url;
  pollState.version = version;
  cameraPollState.set(camera.name, pollState);
  if (!camera.visible || version <= 0) {
    // No frame has ever arrived (camera unplugged/not publishing): the
    // frame endpoint would 404 and the <img> would render the browser's
    // broken-image glyph. An src-less <img> renders nothing; the panel's
    // stale badge already tells the story.
    img.removeAttribute("src");
    return;
  }
  img.src = `${camera.frame_url}?v=${version}&ts=${Date.now()}`;
}

function maybeStartCameraWebRtc(camera, panel) {
  if (!window.RTCPeerConnection || !camera.webrtc_available || !camera.webrtc_port) {
    return;
  }
  if (!camera.visible || camera.stale) {
    // No frames flowing; dialing now would just burn retry attempts
    // waiting on a first frame that cannot arrive.
    return;
  }
  const state = cameraWebRtc.get(camera.name);
  if (state && (state.pc || state.retryTimer || state.unavailable || state.attempts >= WEBRTC_MAX_ATTEMPTS)) {
    return;
  }
  startCameraWebRtc(camera.name, panel, camera.webrtc_port);
}

// webrtc_worker.py (a separate process from this backend, see wiki
// changelog 2026-07-22) serves WebRTC signaling on its own port -- webrtcPort
// is only passed in on the first dial (from the camera-status payload);
// scheduleWebRtcRetry's retries call back in without it, so it falls back
// to whatever the previous attempt already cached in cameraWebRtc's state.
function startCameraWebRtc(cameraName, panel, webrtcPort) {
  if (pageUnloading) {
    return;
  }
  const previous = cameraWebRtc.get(cameraName);
  const port = webrtcPort || (previous && previous.webrtcPort);
  if (!port) {
    return;
  }
  const video = panel.querySelector(".camera-video");
  const img = panel.querySelector("img.camera-frame");
  const state = {
    pc: null,
    ws: null,
    active: false,
    attempts: (previous ? previous.attempts : 0) + 1,
    retryTimer: null,
    unavailable: Boolean(previous && previous.unavailable),
    webrtcPort: port
  };
  cameraWebRtc.set(cameraName, state);
  const wsProtocol = location.protocol === "https:" ? "wss" : "ws";
  const hostname = location.hostname.includes(":") ? `[${location.hostname}]` : location.hostname;
  const ws = new WebSocket(`${wsProtocol}://${hostname}:${port}/ws/webrtc?camera=${encodeURIComponent(cameraName)}`);
  const pc = new RTCPeerConnection();
  state.ws = ws;
  state.pc = pc;
  // Every failure signal funnels here; the state.pc === pc guard makes the
  // late duplicates (onerror then onclose, a stale watchdog) no-ops.
  const fail = () => {
    if (cameraWebRtc.get(cameraName) === state && state.pc === pc) {
      scheduleWebRtcRetry(cameraName, panel);
    }
  };
  const watchdog = window.setTimeout(fail, WEBRTC_FIRST_FRAME_TIMEOUT_MS);
  pc.ontrack = (event) => {
    video.srcObject = event.streams[0];
  };
  pc.onicecandidate = (event) => {
    if (event.candidate && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "ice",
        candidate: event.candidate.candidate,
        sdpMLineIndex: event.candidate.sdpMLineIndex
      }));
    }
  };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "failed" || pc.connectionState === "closed") {
      fail();
    }
  };
  ws.onmessage = async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === "offer") {
      try {
        await pc.setRemoteDescription({ type: "offer", sdp: message.sdp });
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        ws.send(JSON.stringify({ type: "answer", sdp: answer.sdp }));
      } catch {
        // Typically: this browser has no H.264 receiver (vendored kiosk
        // Chromium). Polling keeps the panel alive.
        fail();
      }
    } else if (message.type === "ice" && message.candidate) {
      try {
        await pc.addIceCandidate({ candidate: message.candidate, sdpMLineIndex: message.sdpMLineIndex });
      } catch {
        // Candidates racing a teardown are harmless.
      }
    } else if (message.type === "webrtc_unavailable") {
      state.unavailable = true;
      fail();
    }
  };
  ws.onerror = fail;
  ws.onclose = fail;
  const onVideoFrame = () => {
    if (cameraWebRtc.get(cameraName) !== state || state.pc !== pc) {
      return;
    }
    if (!state.active) {
      state.active = true;
      state.attempts = 0;
      window.clearTimeout(watchdog);
      video.style.display = "";
      img.style.display = "none";
      img.removeAttribute("src");
    }
    recordDisplayedFrame(cameraName);
    video.requestVideoFrameCallback(onVideoFrame);
  };
  if (video.requestVideoFrameCallback) {
    video.requestVideoFrameCallback(onVideoFrame);
  } else {
    // No rVFC (old Firefox): activate on playback start; the fps badge
    // then reflects backend fps only.
    video.addEventListener("playing", onVideoFrame, { once: true });
  }
}

function scheduleWebRtcRetry(cameraName, panel) {
  if (pageUnloading) {
    return;
  }
  const state = cameraWebRtc.get(cameraName);
  if (!state || state.retryTimer) {
    return;
  }
  const wasActive = state.active;
  state.active = false;
  try { if (state.pc) state.pc.close(); } catch {}
  try { if (state.ws) state.ws.close(); } catch {}
  state.pc = null;
  state.ws = null;
  const video = panel.querySelector(".camera-video");
  const img = panel.querySelector("img.camera-frame");
  if (video) {
    video.style.display = "none";
    video.srcObject = null;
  }
  if (img) {
    img.style.display = "";
  }
  if (wasActive) {
    const pollState = cameraPollState.get(cameraName);
    if (pollState) {
      // Force the next poll tick to re-issue the frame URL even if the
      // version has not moved since the WebRTC path took over.
      pollState.version = -1;
    }
  }
  if (state.unavailable || state.attempts >= WEBRTC_MAX_ATTEMPTS) {
    return;
  }
  state.retryTimer = window.setTimeout(() => {
    state.retryTimer = null;
    const currentPanel = cameraPanels.get(cameraName);
    if (currentPanel) {
      startCameraWebRtc(cameraName, currentPanel);
    }
  }, WEBRTC_RETRY_DELAY_MS * Math.max(1, state.attempts));
}

function stopCameraWebRtc(cameraName) {
  const state = cameraWebRtc.get(cameraName);
  if (!state) {
    return;
  }
  if (state.retryTimer) {
    window.clearTimeout(state.retryTimer);
    state.retryTimer = null;
  }
  try { if (state.pc) state.pc.close(); } catch {}
  try { if (state.ws) state.ws.close(); } catch {}
  cameraWebRtc.delete(cameraName);
}

// Prevent an old page's WebRTC retries and sockets from overlapping the
// sessions started by the destination page during navigation.
window.addEventListener("pagehide", () => {
  pageUnloading = true;
  for (const cameraName of Array.from(cameraWebRtc.keys())) {
    stopCameraWebRtc(cameraName);
  }
});

// Recording continues in the backend after the tab closes. Warn before a
// tab/window close or full-page navigation so it is not mistaken for Stop.

function updateCameraFps(cameraName, fps) {
  const pollState = cameraPollState.get(cameraName);
  if (!pollState) {
    return;
  }
  pollState.backendFps = Number.isFinite(fps) ? fps : 0;
  cameraPollState.set(cameraName, pollState);
  renderCameraFps(cameraName);
}

function recordDisplayedFrame(cameraName) {
  const pollState = cameraPollState.get(cameraName);
  if (!pollState) {
    return;
  }
  const now = performance.now();
  const frameTimes = pollState.displayFrameTimes || [];
  frameTimes.push(now);
  const minTime = now - CAMERA_FPS_WINDOW_MS;
  while (frameTimes.length > 0 && frameTimes[0] < minTime) {
    frameTimes.shift();
  }
  pollState.displayFrameTimes = frameTimes;
  cameraPollState.set(cameraName, pollState);
  renderCameraFps(cameraName);
}

function computeDisplayedFps(frameTimes) {
  if (!frameTimes || frameTimes.length < 2) {
    return 0;
  }
  const durationMs = Math.max(frameTimes[frameTimes.length - 1] - frameTimes[0], 1);
  return ((frameTimes.length - 1) * 1000) / durationMs;
}

function renderCameraFps(cameraName) {
  const panel = cameraPanels.get(cameraName);
  if (!panel) {
    return;
  }
  const pollState = cameraPollState.get(cameraName);
  if (!pollState) {
    return;
  }
  const label = panel.querySelector("[data-camera-fps]");
  if (!label) {
    return;
  }
  const displayFps = computeDisplayedFps(pollState.displayFrameTimes);
  const backendFps = Number(pollState.backendFps || 0);
  label.textContent = displayFps > 0 ? `${displayFps.toFixed(1)} fps` : "-- fps";
  label.title = backendFps > 0 ? `rx ${backendFps.toFixed(1)} fps` : "rx -- fps";
}

function normalizeRotation(value) {
  const angle = Number(value || 0);
  return ((angle % 360) + 360) % 360;
}

function toggleCameraMaximized(cameraName) {
  if (maximizedCameraName === cameraName) {
    setCameraMaximized(cameraName, false);
    maximizedCameraName = null;
    return;
  }
  if (maximizedCameraName) {
    setCameraMaximized(maximizedCameraName, false);
  }
  setCameraMaximized(cameraName, true);
  maximizedCameraName = cameraName;
}

function setCameraMaximized(cameraName, maximized) {
  const panel = cameraPanels.get(cameraName);
  if (!panel) {
    return;
  }
  const button = panel.querySelector("[data-camera-maximize]");
  panel.classList.toggle("maximized", maximized);
  button.textContent = maximized ? "❐" : "□";
  button.title = maximized ? "Restore" : "Maximize";
}
