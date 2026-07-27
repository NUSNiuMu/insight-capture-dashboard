import { startCameraDashboard } from "../camera/dashboard.js";
import { escapeHtml } from "../shared/format.js";
import { initializeRosbags } from "../shared/rosbags.js";
import {
  clearKeptTrajectory,
  clearRenderedTrajectories,
  clearMappingVisualization,
  queueMappingUpdate,
  queuePoseUpdate,
  setAvatarLoadStage,
  setKeepTrajectory,
  setMappingVisible,
  setTrajectoriesEnabled,
  stopSpatialRenderer,
} from "../spatial/renderer.js?v=20260727-mapping";

const modelStatus = document.getElementById("model-status");
const alignmentPanel = document.getElementById("alignment-panel");
const alignmentStatus = document.getElementById("alignment-status");
const alignmentMeta = document.getElementById("alignment-meta");
const alignmentToggle = document.getElementById("alignment-toggle");
const playbackPanel = document.getElementById("playback-panel");
const playbackBagSelect = document.getElementById("playback-bag-select");
const startPlaybackButton = document.getElementById("start-playback-button");
const stopPlaybackButton = document.getElementById("stop-playback-button");
const goLiveButton = document.getElementById("go-live-button");
const playbackStatusEl = document.getElementById("playback-status");
const clearTrajectoryButton = document.getElementById("clear-trajectory-button");
const keepTrajectoryToggle = document.getElementById("keep-trajectory-toggle");
const mappingStatus = document.getElementById("mapping-status");
const mappingMeta = document.getElementById("mapping-meta");
const mappingCameraStates = document.getElementById("mapping-camera-states");
const mappingVisibleToggle = document.getElementById("mapping-visible-toggle");
const newMapButton = document.getElementById("new-map-button");
const POSE_STREAM_STALE_MS = 4000;
const wsUrl = resolveWebSocketUrl();
let alignmentBusy = false;
let playbackBusy = false;
let playbackPollTimer = null;
let keepTrajectory = false;
let pageUnloading = false;
let activeWs = null;
let activeMappingWs = null;
let mappingResetBusy = false;
let mappingVisible = true;
let mappingStreamOnline = false;
let playbackActive = false;
let lastPoseMessageAt = 0;
const startupTimers = new Set();

connect();
connectMapping();
scheduleStartup();
window.addEventListener("pagehide", () => {
  pageUnloading = true;
  startupTimers.forEach((timer) => window.clearTimeout(timer));
  startupTimers.clear();
  if (activeWs) {
    try { activeWs.close(); } catch {}
    activeWs = null;
  }
  if (activeMappingWs) {
    try { activeMappingWs.close(); } catch {}
    activeMappingWs = null;
  }
  stopSpatialRenderer();
});

function scheduleStartup() {
  // Keep the viewport responsive before heavier camera, trace, and avatar work.
  scheduleStartupTask(() => {
    fetchAlignmentStatus();
    startCameraDashboard({ cameraStaggerMs: 450 });
  }, 250);
  scheduleStartupTask(() => syncTrajectorySource(), 1200);
  scheduleStartupTask(() => initializeRosbags(), 1500);
  scheduleStartupTask(() => setAvatarLoadStage(1), 1900);
  scheduleStartupTask(() => setAvatarLoadStage(2), 3600);
}

function scheduleStartupTask(callback, delayMs) {
  const timer = window.setTimeout(() => {
    startupTimers.delete(timer);
    if (!pageUnloading) callback();
  }, delayMs);
  startupTimers.add(timer);
}

if (alignmentToggle) {
  alignmentToggle.addEventListener("click", () => { void toggleAlignment(); });
}
if (startPlaybackButton) {
  startPlaybackButton.addEventListener("click", () => { void startPlayback(); });
}
if (stopPlaybackButton) {
  stopPlaybackButton.addEventListener("click", () => { void stopPlayback(); });
}
if (goLiveButton) {
  goLiveButton.addEventListener("click", () => { void goLive(); });
}
if (clearTrajectoryButton) {
  clearTrajectoryButton.addEventListener("click", () => { void clearAllTrajectories(); });
}
if (keepTrajectoryToggle) {
  keepTrajectoryToggle.addEventListener("click", () => {
    keepTrajectory = !keepTrajectory;
    setKeepTrajectory(keepTrajectory);
    keepTrajectoryToggle.setAttribute("aria-pressed", String(keepTrajectory));
    keepTrajectoryToggle.classList.toggle("is-active", keepTrajectory);
  });
}
if (mappingVisibleToggle) {
  mappingVisibleToggle.addEventListener("click", () => {
    mappingVisible = !mappingVisible;
    syncTrajectorySource();
    mappingVisibleToggle.setAttribute("aria-pressed", String(mappingVisible));
    mappingVisibleToggle.classList.toggle("is-active", mappingVisible);
    mappingVisibleToggle.textContent = mappingVisible ? "Map visible" : "Map hidden";
  });
}
if (newMapButton) {
  newMapButton.addEventListener("click", () => { void resetMapping(); });
}
if (playbackPanel) {
  void refreshPlaybackStatus();
  playbackPollTimer = window.setInterval(() => { void refreshPlaybackStatus(); }, 1500);
}

function resolveWebSocketUrl() {
  const query = new URLSearchParams(window.location.search);
  const explicit = query.get("ws");
  if (explicit) {
    return explicit;
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host || "localhost:8765";
  return `${protocol}//${host}/ws`;
}

function connect() {
  if (modelStatus) {
    modelStatus.textContent = "Connecting pose stream...";
  }
  const ws = new WebSocket(wsUrl);
  activeWs = ws;
  lastPoseMessageAt = Date.now();

  ws.onopen = () => {
    if (modelStatus) {
      modelStatus.textContent = "Pose stream connected";
    }
  };

  ws.onmessage = (event) => {
    lastPoseMessageAt = Date.now();
    const payload = JSON.parse(event.data);
    if (payload.alignment) {
      renderAlignment(payload.alignment);
    }
    if (payload.type !== "pose_update") {
      return;
    }
    if (!queuePoseUpdate(payload)) {
      ws.close();
    }
  };

  ws.onerror = () => {
    if (modelStatus) {
      modelStatus.textContent = "Pose stream error";
    }
  };

  ws.onclose = () => {
    if (activeWs === ws) {
      activeWs = null;
    }
    if (pageUnloading) {
      return;
    }
    if (modelStatus) {
      modelStatus.textContent = "Pose stream disconnected, retrying...";
    }
    window.setTimeout(connect, 1000);
  };
}

function connectMapping() {
  if (mappingStatus) mappingStatus.textContent = "Connecting mapping stream...";
  const ws = new WebSocket(resolveMappingWebSocketUrl());
  activeMappingWs = ws;
  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type !== "mapping_update") return;
    queueMappingUpdate(payload);
    renderMappingStatus(payload);
  };
  ws.onerror = () => {
    if (mappingStatus) mappingStatus.textContent = "Mapping stream error";
  };
  ws.onclose = () => {
    if (activeMappingWs === ws) activeMappingWs = null;
    if (pageUnloading) return;
    mappingStreamOnline = false;
    syncTrajectorySource();
    if (mappingStatus) mappingStatus.textContent = "Mapping disconnected, retrying...";
    window.setTimeout(connectMapping, 1000);
  };
}

function resolveMappingWebSocketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host || "localhost:8765";
  return `${protocol}//${host}/ws/mapping`;
}

async function resetMapping() {
  if (mappingResetBusy || !newMapButton) return;
  mappingResetBusy = true;
  newMapButton.disabled = true;
  newMapButton.textContent = "Resetting...";
  clearMappingVisualization();
  try {
    const response = await fetch("/api/mapping/reset", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      const unavailable = (payload.unavailable || []).join(", ");
      throw new Error(`Mapping service unavailable: ${unavailable || "unknown"}`);
    }
    if (payload.mapping) renderMappingStatus(payload.mapping);
  } catch (error) {
    if (mappingMeta) {
      mappingMeta.textContent = error instanceof Error ? error.message : String(error);
    }
  } finally {
    mappingResetBusy = false;
    newMapButton.disabled = false;
    newMapButton.textContent = "New map";
  }
}

function renderMappingStatus(payload) {
  const statuses = payload.statuses || {};
  const mapper = statuses.insight9 || {};
  mappingStreamOnline = Boolean(mapper.online);
  syncTrajectorySource();
  const onlineCount = Object.values(statuses).filter((status) => status.online).length;
  const points = Number(payload.map_point_count || 0);
  if (mappingStatus) {
    mappingStatus.textContent = mapper.online
      ? `${points.toLocaleString()} confirmed map points`
      : "Mapping services offline";
  }
  if (mappingMeta) {
    const keyframe = Number(mapper.keyframe || 0);
    const promoted = Number(mapper.promoted || 0);
    mappingMeta.textContent =
      `${onlineCount}/3 streams online · keyframe ${keyframe} · last promoted ${promoted}`;
  }
  if (mappingCameraStates) {
    const labels = {
      insight9: "Insight9 map",
      insight3_a: "Insight3 A",
      insight3_b: "Insight3 B",
    };
    mappingCameraStates.innerHTML = Object.entries(labels).map(([name, label]) => {
      const status = statuses[name] || {};
      const state = status.online ? String(status.state || "online") : "offline";
      const active = status.online && (name === "insight9" || Boolean(status.localized));
      return `<span class="${active ? "is-ok" : ""}"><i></i>${escapeHtml(label)} · ${escapeHtml(state)}</span>`;
    }).join("");
  }
}

window.setInterval(() => {
  if (!activeWs || activeWs.readyState !== WebSocket.OPEN) {
    return;
  }
  if (Date.now() - lastPoseMessageAt > POSE_STREAM_STALE_MS) {
    activeWs.close();
  }
}, 1000);

async function fetchAlignmentStatus() {
  if (!alignmentPanel) {
    return;
  }
  try {
    const response = await fetch(`/api/alignment?ts=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    if (payload && payload.alignment) {
      renderAlignment(payload.alignment);
    }
  } catch (_error) {
    // The websocket will refresh status once connected.
  }
}

async function toggleAlignment() {
  if (!alignmentToggle || alignmentBusy) {
    return;
  }
  const shouldStop = alignmentToggle.dataset.action === "stop";
  alignmentBusy = true;
  syncAlignmentButtonState();
  try {
    const response = await fetch(shouldStop ? "/api/alignment/stop" : "/api/alignment/start", {
      method: "POST"
    });
    const payload = await response.json();
    if (payload && payload.alignment) {
      renderAlignment(payload.alignment);
    }
  } catch (_error) {
    if (alignmentMeta) {
      alignmentMeta.textContent = "Alignment control request failed";
    }
  } finally {
    alignmentBusy = false;
    syncAlignmentButtonState();
  }
}

function renderAlignment(alignment) {
  if (!alignmentPanel) {
    return;
  }
  const available = Boolean(alignment && alignment.available);
  const active = Boolean(alignment && alignment.active);
  const statusText = (alignment && alignment.status_text) || "Alignment OFF";
  const requiredSamples = Number((alignment && alignment.required_samples) || 0);
  const inlierCount = Number((alignment && alignment.inlier_count) || 0);
  const visibleCameras = Number((alignment && alignment.visible_cameras) || 0);
  const cameraCount = Number((alignment && alignment.camera_count) || 0);
  const hasSolution = Boolean(alignment && alignment.has_solution);
  const lockOnFirst = Boolean(alignment && alignment.lock_on_first_solution);

  if (alignmentStatus) {
    alignmentStatus.textContent = statusText;
  }
  if (alignmentToggle) {
    alignmentToggle.dataset.action = active ? "stop" : "start";
    alignmentToggle.dataset.state = active ? "stop" : "start";
    alignmentToggle.textContent = active ? "Stop Alignment" : "Start Alignment";
    alignmentToggle.disabled = !available || alignmentBusy;
  }
  if (alignmentMeta) {
    if (!available) {
      alignmentMeta.textContent = "Alignment stream unavailable in this backend session";
    } else if (active) {
      alignmentMeta.textContent =
        `Board ${visibleCameras}/${cameraCount} visible · samples ${inlierCount}/${requiredSamples}` +
        (lockOnFirst ? " · auto-lock after first camera is ON" : " · manual stop mode");
    } else if (hasSolution) {
      alignmentMeta.textContent = "Last calibration remains applied to the 3D view. Press Start Alignment to recalibrate.";
    } else {
      alignmentMeta.textContent = "Ready to calibrate from the web view. Press Start Alignment when the board is visible.";
    }
  }
  syncAlignmentButtonState();
}

function syncAlignmentButtonState() {
  if (!alignmentToggle) {
    return;
  }
  if (alignmentBusy) {
    alignmentToggle.disabled = true;
    alignmentToggle.classList.add("is-busy");
    alignmentToggle.textContent = alignmentToggle.dataset.action === "stop" ? "Stopping..." : "Starting...";
    return;
  }
  alignmentToggle.classList.remove("is-busy");
}


async function startPlayback() {
  if (playbackBusy) return;
  const bagName = playbackBagSelect ? playbackBagSelect.value : "";
  if (!bagName) {
    if (playbackStatusEl) playbackStatusEl.textContent = "No bag selected.";
    return;
  }
  playbackBusy = true;
  if (startPlaybackButton) startPlaybackButton.disabled = true;
  if (playbackStatusEl) playbackStatusEl.textContent = "Starting playback...";
  try {
    const response = await fetch("/api/playback/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bag_name: bagName }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to start playback.");
    clearKeptTrajectory();
    renderPlaybackStatus(payload);
  } catch (error) {
    if (playbackStatusEl) playbackStatusEl.textContent = error instanceof Error ? error.message : String(error);
    if (startPlaybackButton) startPlaybackButton.disabled = false;
  } finally {
    playbackBusy = false;
  }
}

async function stopPlayback() {
  if (playbackBusy) return;
  playbackBusy = true;
  if (stopPlaybackButton) stopPlaybackButton.disabled = true;
  try {
    const response = await fetch("/api/playback/stop", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to stop playback.");
    renderPlaybackStatus({ state: "idle", bag_name: "" });
  } catch (error) {
    if (playbackStatusEl) playbackStatusEl.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    playbackBusy = false;
    if (stopPlaybackButton) stopPlaybackButton.disabled = false;
  }
}

async function goLive() {
  if (playbackBusy) return;
  playbackBusy = true;
  if (goLiveButton) goLiveButton.disabled = true;
  try {
    await fetch("/api/playback/stop", { method: "POST" });
    await fetch("/api/trajectory/clear", { method: "POST" });
    clearRenderedTrajectories();
    renderPlaybackStatus({ state: "idle", bag_name: "" });
  } catch (error) {
    if (playbackStatusEl) playbackStatusEl.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    playbackBusy = false;
    if (goLiveButton) goLiveButton.disabled = false;
  }
}

async function refreshPlaybackStatus() {
  if (!playbackPanel) return;
  try {
    const response = await fetch(`/api/playback/status?ts=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    renderPlaybackStatus(payload);
  } catch (_) {
    // ignore network errors during polling
  }
}

function renderPlaybackStatus(payload) {
  const state = (payload && payload.state) || "idle";
  const bagName = (payload && payload.bag_name) || "";
  const isPlaying = state === "playing";
  playbackActive = isPlaying;
  syncTrajectorySource();
  if (startPlaybackButton) {
    startPlaybackButton.hidden = isPlaying;
    if (!isPlaying) startPlaybackButton.disabled = false;
  }
  if (stopPlaybackButton) stopPlaybackButton.hidden = !isPlaying;
  if (goLiveButton) goLiveButton.hidden = !isPlaying;
  if (playbackBagSelect) playbackBagSelect.disabled = isPlaying;
  if (playbackStatusEl) {
    playbackStatusEl.textContent = isPlaying ? `Playing: ${bagName}` : "Idle";
  }
}

function syncTrajectorySource() {
  const useGlobalMapping = mappingStreamOnline && !playbackActive;
  document.body.classList.toggle("global-mapping-active", useGlobalMapping);
  setTrajectoriesEnabled(!useGlobalMapping);
  setMappingVisible(mappingVisible && !playbackActive);
}

async function clearAllTrajectories() {
  clearRenderedTrajectories();
  try {
    await fetch("/api/trajectory/clear", { method: "POST" });
  } catch (_) {
    // best-effort
  }
}
