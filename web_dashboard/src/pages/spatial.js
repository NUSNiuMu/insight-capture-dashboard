import {
  setCameraCapturePerformanceMode,
  startPreparedCameraPlayback,
  startCameraDashboard,
  stopPreparedCameraPlayback,
} from "../camera/dashboard.js?v=20260813-playback-wall";
import { escapeHtml } from "../shared/format.js";
import { initializeRosbags } from "../shared/rosbags.js?v=20260812-review-bundle-v1";
import {
  clearKeptTrajectory,
  clearRenderedTrajectories,
  beginPreparedPlayback,
  endPreparedPlayback,
  queuePoseUpdate,
  setAvatarLoadStage,
  setCapturePerformanceMode,
  setKeepTrajectory,
  setTrajectoriesEnabled,
  stopSpatialRenderer,
} from "../spatial/renderer.js?v=20260813-native-spatial-resolution";

const modelStatus = document.getElementById("model-status");
const playbackPanel = document.getElementById("playback-panel");
const playbackBagSelect = document.getElementById("playback-bag-select");
const prebuildReviewsButton = document.getElementById("prebuild-reviews-button");
const startPlaybackButton = document.getElementById("start-playback-button");
const stopPlaybackButton = document.getElementById("stop-playback-button");
const goLiveButton = document.getElementById("go-live-button");
const playbackStatusEl = document.getElementById("playback-status");
const playbackProgressEl = document.getElementById("playback-prepare-progress");
const playbackProgressStageEl = document.getElementById("playback-progress-stage");
const playbackProgressPercentEl = document.getElementById("playback-progress-percent");
const playbackProgressBar = document.getElementById("playback-progress-bar");
const clearTrajectoryButton = document.getElementById("clear-trajectory-button");
const keepTrajectoryToggle = document.getElementById("keep-trajectory-toggle");
const mappingStatus = document.getElementById("mapping-status");
const mappingMeta = document.getElementById("mapping-meta");
const mappingCameraStates = document.getElementById("mapping-camera-states");
const newMapButton = document.getElementById("new-map-button");
const obsModeToggle = document.getElementById("obs-mode-toggle");
const POSE_STREAM_STALE_MS = 4000;
const OBS_MODE_STORAGE_KEY = "insight.obs-performance-mode";
const wsUrl = resolveWebSocketUrl();
let playbackBusy = false;
let playbackPollTimer = null;
let mappingPollTimer = null;
let keepTrajectory = false;
let pageUnloading = false;
let activeWs = null;
let mappingResetBusy = false;
let lastPoseMessageAt = 0;
let obsModeEnabled = readInitialObsMode();
let latestLivePosePayload = null;
let preparedManifest = null;
let preparedPlaybackActive = false;
let preparedPlaybackStarting = false;
let playbackRequested = false;
const startupTimers = new Set();

applyObsMode(obsModeEnabled);
// The first WebSocket message is the only unconditional trace snapshot.
// Enable trajectories before connecting so startup staging cannot discard it.
setTrajectoriesEnabled(true);
connect();
scheduleStartup();
window.addEventListener("pagehide", () => {
  pageUnloading = true;
  const wasPreparedPlayback = preparedPlaybackActive || preparedPlaybackStarting;
  stopPreparedCameraPlayback();
  endPreparedPlayback();
  if (wasPreparedPlayback) navigator.sendBeacon("/api/playback/stop");
  startupTimers.forEach((timer) => window.clearTimeout(timer));
  startupTimers.clear();
  if (activeWs) {
    try { activeWs.close(); } catch {}
    activeWs = null;
  }
  if (playbackPollTimer) window.clearInterval(playbackPollTimer);
  if (mappingPollTimer) window.clearInterval(mappingPollTimer);
  stopSpatialRenderer();
});

function scheduleStartup() {
  // Keep the viewport responsive before heavier camera, trace, and avatar work.
  scheduleStartupTask(() => {
    startCameraDashboard({ cameraStaggerMs: 450 });
    void refreshMappingStatus();
    mappingPollTimer = window.setInterval(() => { void refreshMappingStatus(); }, 500);
  }, 250);
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

if (startPlaybackButton) {
  startPlaybackButton.addEventListener("click", () => { void startPlayback(); });
}
if (prebuildReviewsButton) {
  prebuildReviewsButton.addEventListener("click", () => { void prebuildAllReviews(); });
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
if (newMapButton) {
  newMapButton.addEventListener("click", () => { void resetMapping(); });
}
if (obsModeToggle) {
  obsModeToggle.addEventListener("click", () => {
    obsModeEnabled = !obsModeEnabled;
    window.localStorage.setItem(OBS_MODE_STORAGE_KEY, obsModeEnabled ? "1" : "0");
    applyObsMode(obsModeEnabled);
  });
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

function readInitialObsMode() {
  const query = new URLSearchParams(window.location.search);
  if (query.has("obs")) {
    return query.get("obs") !== "0";
  }
  return window.localStorage.getItem(OBS_MODE_STORAGE_KEY) === "1";
}

function applyObsMode(enabled) {
  document.body.classList.toggle("capture-performance", enabled);
  setCapturePerformanceMode(enabled);
  setCameraCapturePerformanceMode(enabled);
  if (obsModeToggle) {
    obsModeToggle.classList.toggle("is-active", enabled);
    obsModeToggle.setAttribute("aria-pressed", String(enabled));
    obsModeToggle.textContent = enabled ? "OBS mode: on" : "OBS mode";
  }
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
    if (payload.type !== "pose_update") {
      return;
    }
    latestLivePosePayload = payload;
    if (preparedPlaybackActive) return;
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

async function resetMapping() {
  if (mappingResetBusy || !newMapButton) return;
  mappingResetBusy = true;
  newMapButton.disabled = true;
  newMapButton.textContent = "Resetting...";
  clearRenderedTrajectories();
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

async function refreshMappingStatus() {
  try {
    const response = await fetch(`/api/mapping?ts=${Date.now()}`, {
      cache: "no-store",
    });
    if (!response.ok) return;
    renderMappingStatus(await response.json());
  } catch (_error) {
    if (mappingStatus) mappingStatus.textContent = "Mapping status unavailable";
  }
}

function renderMappingStatus(payload) {
  const statuses = payload.statuses || {};
  const mapper = statuses.insight9 || {};
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
    const loops = Number(mapper.loop_closures || 0);
    const loopProgress = Number(mapper.loop_confirmation_progress || 0);
    const loopRequired = Number(mapper.loop_confirmation_required || 0);
    const loopCheck = loopProgress > 0 && loopRequired > 0
      ? ` · loop check ${loopProgress}/${loopRequired}`
      : "";
    mappingMeta.textContent =
      `${onlineCount}/3 streams online · keyframe ${keyframe} · ` +
      `last promoted ${promoted} · loops ${loops}${loopCheck}`;
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

async function startPlayback() {
  if (playbackBusy) return;
  const bagName = playbackBagSelect ? playbackBagSelect.value : "";
  if (!bagName) {
    if (playbackStatusEl) playbackStatusEl.textContent = "No bag selected.";
    return;
  }
  playbackBusy = true;
  playbackRequested = true;
  renderPlaybackProgress(true, 0, "Checking review bundle");
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
    if (payload.state === "ready") void startPreparedPlayback(payload);
  } catch (error) {
    playbackRequested = false;
    renderPlaybackProgress(false, 0, "Encoding");
    if (playbackStatusEl) playbackStatusEl.textContent = error instanceof Error ? error.message : String(error);
    if (startPlaybackButton) startPlaybackButton.disabled = false;
  } finally {
    playbackBusy = false;
  }
}

async function prebuildAllReviews() {
  if (!prebuildReviewsButton || prebuildReviewsButton.disabled) return;
  prebuildReviewsButton.disabled = true;
  try {
    const response = await fetch("/api/playback/prebuild", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ all: true }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not queue review bundles.");
    renderPlaybackStatus(payload);
  } catch (error) {
    if (playbackStatusEl) playbackStatusEl.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    prebuildReviewsButton.disabled = false;
  }
}

async function stopPlayback() {
  if (playbackBusy) return;
  playbackBusy = true;
  playbackRequested = false;
  preparedPlaybackStarting = false;
  preparedPlaybackActive = false;
  preparedManifest = null;
  stopPreparedCameraPlayback();
  endPreparedPlayback();
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
  playbackRequested = false;
  preparedPlaybackStarting = false;
  preparedPlaybackActive = false;
  preparedManifest = null;
  stopPreparedCameraPlayback();
  endPreparedPlayback();
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
    if (payload.state === "ready" && playbackRequested) {
      void startPreparedPlayback(payload);
    }
  } catch (_) {
    // ignore network errors during polling
  }
}

function renderPlaybackStatus(payload) {
  const state = (payload && payload.state) || "idle";
  const bagName = (payload && payload.bag_name) || "";
  const queued = Array.isArray(payload?.queued) ? payload.queued.length : 0;
  const isBackground = Boolean(payload?.background) && !playbackRequested;
  const isPlaying = state === "playing";
  const isPreparing = (state === "preparing" && !isBackground) || (state === "ready" && playbackRequested);
  const isBusy = isPlaying || isPreparing;
  if (startPlaybackButton) {
    startPlaybackButton.hidden = isBusy;
    if (!isBusy) startPlaybackButton.disabled = false;
  }
  if (stopPlaybackButton) stopPlaybackButton.hidden = !isBusy;
  if (goLiveButton) goLiveButton.hidden = !isPlaying;
  if (prebuildReviewsButton) prebuildReviewsButton.hidden = isPlaying;
  if (playbackBagSelect) playbackBagSelect.disabled = isBusy;
  if (state === "preparing") {
    renderPlaybackProgress(true, Number(payload.progress || 0), payload.stage || "Encoding");
  } else if (state === "ready" && playbackRequested) {
    renderPlaybackProgress(true, 1, "Loading prepared media");
  } else {
    renderPlaybackProgress(false, 0, "Encoding");
  }
  if (playbackStatusEl) {
    if (state === "preparing") {
      const progress = Math.round(Number(payload.progress || 0) * 100);
      playbackStatusEl.textContent = `${isBackground ? "Background review" : "Preparing"} ${progress}% · ${payload.stage || bagName}${queued ? ` · ${queued} queued` : ""}`;
    } else if (state === "ready" && playbackRequested) {
      playbackStatusEl.textContent = `Loading prepared media: ${bagName}`;
    } else if (state === "ready") {
      playbackStatusEl.textContent = `Prepared cache ready: ${bagName}`;
    } else if (state === "error") {
      playbackStatusEl.textContent = payload.error || "Playback preparation failed.";
    } else {
      playbackStatusEl.textContent = isPlaying
        ? `Playing prepared: ${bagName}${preparedQualitySummary()}`
        : queued ? `Idle · ${queued} reviews queued` : "Idle";
    }
  }
}

function renderPlaybackProgress(visible, progress, stage) {
  if (!playbackProgressEl || !playbackProgressBar) return;
  const percentage = Math.max(0, Math.min(100, Math.round(Number(progress || 0) * 100)));
  playbackProgressEl.hidden = !visible;
  playbackProgressEl.setAttribute("aria-valuenow", String(percentage));
  playbackProgressBar.value = percentage;
  playbackProgressBar.textContent = `${percentage}%`;
  if (playbackProgressPercentEl) playbackProgressPercentEl.textContent = `${percentage}%`;
  if (playbackProgressStageEl) {
    playbackProgressStageEl.textContent = stage || "Encoding";
    playbackProgressStageEl.title = stage || "Encoding";
  }
}

function preparedQualitySummary() {
  if (!preparedManifest) return "";
  const cameras = Array.isArray(preparedManifest.source_cameras)
    ? preparedManifest.source_cameras
    : Array.isArray(preparedManifest.cameras) ? preparedManifest.cameras : [];
  const repeats = cameras.reduce(
    (total, camera) => total + Number(camera.duplicate_frames || 0),
    0
  );
  const maxSkewMs = Math.max(0, ...cameras.map((camera) => Number(camera.max_skew_ms || 0)));
  const quality = String(preparedManifest.quality?.state || "").toUpperCase();
  return ` · ${Number(preparedManifest.fps || 30)} Hz${quality ? ` · ${quality}` : ""} · ${repeats} repeats · max skew ${maxSkewMs.toFixed(1)} ms`;
}

async function startPreparedPlayback(status) {
  if (preparedPlaybackStarting || preparedPlaybackActive || !playbackRequested) return;
  preparedPlaybackStarting = true;
  try {
    const response = await fetch(status.manifest_url, { cache: "force-cache" });
    if (!response.ok) throw new Error("Prepared playback manifest is unavailable.");
    preparedManifest = await response.json();
    beginPreparedPlayback(buildPreparedPosePayload(0, true));
    const activate = await fetch("/api/playback/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bag_name: preparedManifest.bag_name }),
    });
    if (!activate.ok) {
      const payload = await activate.json();
      throw new Error(payload.error || "Could not activate prepared playback.");
    }
    preparedPlaybackActive = true;
    await startPreparedCameraPlayback(preparedManifest, {
      onFrame: (frameIndex) => {
        if (preparedPlaybackActive) queuePoseUpdate(buildPreparedPosePayload(frameIndex, false));
      },
      onEnded: () => { void stopPlayback(); },
    });
    renderPlaybackStatus({ state: "playing", bag_name: preparedManifest.bag_name });
  } catch (error) {
    preparedPlaybackActive = false;
    playbackRequested = false;
    stopPreparedCameraPlayback();
    endPreparedPlayback();
    await fetch("/api/playback/stop", { method: "POST" }).catch(() => {});
    if (playbackStatusEl) {
      playbackStatusEl.textContent = error instanceof Error ? error.message : String(error);
    }
  } finally {
    preparedPlaybackStarting = false;
  }
}

function buildPreparedPosePayload(frameIndex, snapshot) {
  const manifest = preparedManifest;
  const baseByName = new Map((latestLivePosePayload?.poses || []).map((pose) => [pose.name, pose]));
  const poses = (manifest.poses || []).map((pose) => {
    const base = baseByName.get(pose.name) || {};
    const trace = Array.isArray(pose.trajectory) ? pose.trajectory : [];
    const toSeq = trace.length;
    return {
      ...base,
      name: pose.name,
      role: pose.role,
      visible: Boolean(pose.valid?.[frameIndex]),
      position: pose.positions?.[frameIndex] || [0, 0, 0],
      quaternion_xyzw: pose.quaternions_xyzw?.[frameIndex] || [0, 0, 0, 1],
      avatar_model: pose.avatar_model,
      avatar_scale: pose.avatar_scale,
      avatar_rotation_deg_xyz: pose.avatar_rotation_deg_xyz,
      avatar_offset_xyz: pose.avatar_offset_xyz,
      asset_url: pose.avatar_model ? `/asset?path=${encodeURIComponent(pose.avatar_model)}` : null,
      trace_update: {
        mode: snapshot ? "snapshot" : "delta",
        generation: 1,
        from_seq: snapshot ? 1 : toSeq + 1,
        to_seq: toSeq,
        drop_before_seq: 1,
        points: snapshot ? trace : [],
      },
    };
  });
  return {
    type: "pose_update",
    stick_figure_mode: Boolean(latestLivePosePayload?.stick_figure_mode),
    display_fps_limit: Number(manifest.fps || 30),
    trace_capacity: Math.max(2, ...poses.map((pose) => pose.trace_update.to_seq)),
    trace_generation: 1,
    poses,
  };
}

async function clearAllTrajectories() {
  clearRenderedTrajectories();
  try {
    await fetch("/api/trajectory/clear", { method: "POST" });
  } catch (_) {
    // best-effort
  }
}
