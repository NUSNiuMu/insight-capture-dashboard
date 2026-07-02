const dashboardView = document.body.dataset.dashboardView || "full";
const enable3d = dashboardView === "full" || dashboardView === "3d";
const enableImages = dashboardView === "images";
const enableCameras = dashboardView === "full" || dashboardView === "cameras" || enableImages;

const canvas = document.getElementById("render-canvas");
const modelStatus = document.getElementById("model-status");
const legend = document.getElementById("pose-legend");
const cameraDock = document.getElementById("camera-dock");
const cameraPageMeta = document.getElementById("camera-page-meta");
const alignmentPanel = document.getElementById("alignment-panel");
const alignmentStatus = document.getElementById("alignment-status");
const alignmentMeta = document.getElementById("alignment-meta");
const alignmentToggle = document.getElementById("alignment-toggle");
const recordingPanel = document.getElementById("recording-panel");
const recordingStatus = document.getElementById("recording-status");
const startRecordingButton = document.getElementById("start-recording-button");
const stopRecordingButton = document.getElementById("stop-recording-button");
const syncRecordingButton = document.getElementById("sync-recording-button");
const refreshRecordTopicsButton = document.getElementById("refresh-record-topics-button");
const recordTopicStatus = document.getElementById("record-topic-status");
const recordSyncStatus = document.getElementById("record-sync-status");
const recordTopicGroups = document.getElementById("record-topic-groups");
const recordingOutput = document.getElementById("recording-output");
const bagList = document.getElementById("bag-list");
const bagListStatus = document.getElementById("bag-list-status");
const refreshBagsButton = document.getElementById("refresh-bags-button");
const playbackPanel = document.getElementById("playback-panel");
const playbackBagSelect = document.getElementById("playback-bag-select");
const startPlaybackButton = document.getElementById("start-playback-button");
const stopPlaybackButton = document.getElementById("stop-playback-button");
const goLiveButton = document.getElementById("go-live-button");
const playbackStatusEl = document.getElementById("playback-status");
const clearTrajectoryButton = document.getElementById("clear-trajectory-button");
const keepTrajectoryToggle = document.getElementById("keep-trajectory-toggle");
const scoringBagMeta = document.getElementById("scoring-bag-meta");
const optimizationBagMeta = document.getElementById("optimization-bag-meta");
const optimizationCameraSelect = document.getElementById("optimization-camera-group");
const optimizationRunNameInput = document.getElementById("optimization-run-name");
const startOptimizationButton = document.getElementById("start-optimization-button");
const stopOptimizationButton = document.getElementById("stop-optimization-button");
const optimizationStepLabel = document.getElementById("optimization-step-label");
const optimizationLogEl = document.getElementById("optimization-log");
const optimizationResultPanel = document.getElementById("optimization-result-panel");
const optimizationLogLink = document.getElementById("optimization-log-link");
const runScoringButton = document.getElementById("run-scoring-button");
const scoringTopicInput = document.getElementById("scoring-topic");
const scoringRefCovInput = document.getElementById("scoring-ref-cov");
const scoringStatusEyebrow = document.getElementById("scoring-status-eyebrow");
const scoringStatusEl = document.getElementById("scoring-status");
const scoringResultEl = document.getElementById("scoring-result");
const scoringResultBody = document.getElementById("scoring-result-body");
const imageCapabilityStatus = document.getElementById("image-capability-status");
const imageCapabilityList = document.getElementById("image-capability-list");
const imagePipelineNotes = document.getElementById("image-pipeline-notes");
const refreshImageCapabilitiesButton = document.getElementById("refresh-image-capabilities-button");

const ROLE_STYLE = {
  head: { label: "Head", color: "#79c47b", primitive: "sphere", modelColor: "#b99572" },
  left_hand: { label: "Left Hand", color: "#79adc2", primitive: "box", modelColor: "#9f8569" },
  right_hand: { label: "Right Hand", color: "#cf7f6f", primitive: "box", modelColor: "#9f8569" }
};
const TRAIL_RADIUS_BY_ROLE = {
  head: 0.01,
  left_hand: 0.008,
  right_hand: 0.008
};

const wsUrl = resolveWebSocketUrl();
const engine = enable3d && canvas ? new BABYLON.Engine(canvas, true, { preserveDrawingBuffer: true, stencil: true }) : null;
const scene = engine && canvas ? createScene(engine, canvas) : null;
const poseNodes = new Map();
const modelPromises = new Map();
const modelWarnings = new Set();
const trailStates = new Map();
const cameraPanels = new Map();
const cameraPollState = new Map();
let maximizedCameraName = null;
let alignmentBusy = false;
let recordingBusy = false;
let scoringBusy = false;
let scoringPollTimer = null;
let recordTopicRefreshBusy = false;
let selectedRecordTopics = new Set();
let knownRecordTopics = new Set();
let recordTopicsInitialized = false;
let recordingLogLines = [];
let knownRosbags = [];
let playbackBusy = false;
let playbackPollTimer = null;
let keepTrajectory = false;
let optimizationBusy = false;
let optimizationPollTimer = null;
const keptPoints = new Map();

const CAMERA_FPS_WINDOW_MS = 1500;
const CAMERA_POLL_INTERVAL_MS = 100;
const DEFAULT_TRAIL_ENABLED = {
  head: true,
  left_hand: true,
  right_hand: true
};

if (engine && scene) {
  engine.runRenderLoop(() => {
    updateTrails();
    scene.render();
  });

  window.addEventListener("resize", () => engine.resize());

}

if (enable3d) {
  connect();
  fetchAlignmentStatus();
}
if (enableCameras) {
  startCameraPolling();
}
if (enableImages) {
  void refreshImageCapabilities();
}
if (recordingPanel) {
  void refreshRecordingStatus({ refreshTopics: true, force: true });
  window.setInterval(() => {
    void refreshRecordingStatus({ refreshTopics: false });
  }, 1500);
}
if (bagList || document.querySelector("[data-bag-select]")) {
  void refreshRosbags();
}
if (runScoringButton) {
  void pollScoringStatus();
}
if (alignmentToggle) {
  alignmentToggle.addEventListener("click", () => {
    void toggleAlignment();
  });
}
if (refreshRecordTopicsButton) {
  refreshRecordTopicsButton.addEventListener("click", () => {
    void refreshRecordTopics({ resetSelection: true });
  });
}
if (startRecordingButton) {
  startRecordingButton.addEventListener("click", () => {
    void startRecording();
  });
}
if (stopRecordingButton) {
  stopRecordingButton.addEventListener("click", () => {
    void stopRecording();
  });
}
if (syncRecordingButton) {
  syncRecordingButton.addEventListener("click", () => {
    void syncRecordingToHost();
  });
}
if (refreshBagsButton) {
  refreshBagsButton.addEventListener("click", () => {
    void refreshRosbags();
  });
}
if (runScoringButton) {
  runScoringButton.addEventListener("click", () => {
    void runScoring();
  });
}
if (refreshImageCapabilitiesButton) {
  refreshImageCapabilitiesButton.addEventListener("click", () => {
    void refreshImageCapabilities();
  });
}
if (startPlaybackButton) {
  startPlaybackButton.addEventListener("click", () => {
    void startPlayback();
  });
}
if (stopPlaybackButton) {
  stopPlaybackButton.addEventListener("click", () => {
    void stopPlayback();
  });
}
if (goLiveButton) {
  goLiveButton.addEventListener("click", () => {
    void goLive();
  });
}
if (clearTrajectoryButton) {
  clearTrajectoryButton.addEventListener("click", () => {
    void clearAllTrajectories();
  });
}
if (keepTrajectoryToggle) {
  keepTrajectoryToggle.addEventListener("click", () => {
    keepTrajectory = !keepTrajectory;
    keepTrajectoryToggle.setAttribute("aria-pressed", String(keepTrajectory));
    keepTrajectoryToggle.classList.toggle("is-active", keepTrajectory);
    if (!keepTrajectory) {
      keptPoints.clear();
    }
  });
}
if (playbackPanel) {
  void refreshPlaybackStatus();
  playbackPollTimer = window.setInterval(() => {
    void refreshPlaybackStatus();
  }, 1500);
}
if (startOptimizationButton) {
  startOptimizationButton.addEventListener("click", () => { void startOptimization(); });
}
if (stopOptimizationButton) {
  stopOptimizationButton.addEventListener("click", () => { void stopOptimization(); });
}
if (optimizationCameraSelect !== null) {
  void populateOptimizationCameras([]);
  void refreshOptimizationStatus();
  void populateOptimizationRuns();
  optimizationPollTimer = window.setInterval(() => { void refreshOptimizationStatus(); }, 2000);
}
const loadOptRunButton = document.getElementById("load-opt-run-button");
if (loadOptRunButton) {
  loadOptRunButton.addEventListener("click", () => { void loadSavedOptRun(); });
}
if (optimizationRunNameInput !== null) {
  optimizationRunNameInput.addEventListener("input", () => {
    optimizationRunNameInput.dataset.autoFilled = "";
  });
  const bagSel = document.getElementById("optimization-bag-select");
  if (bagSel) {
    bagSel.addEventListener("change", updateAutoRunName);
  }
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

function createScene(engineRef, canvasRef) {
  const sceneRef = new BABYLON.Scene(engineRef);
  sceneRef.clearColor = new BABYLON.Color4(0.03, 0.08, 0.11, 1.0);

  const camera = new BABYLON.ArcRotateCamera("camera", -1.2, 1.1, 5.8, new BABYLON.Vector3(0, 0.9, 0), sceneRef);
  camera.attachControl(canvasRef, true);
  camera.wheelDeltaPercentage = 0.015;
  camera.lowerRadiusLimit = 1.5;
  camera.upperRadiusLimit = 18;

  const hemi = new BABYLON.HemisphericLight("hemi", new BABYLON.Vector3(0, 1, 0), sceneRef);
  hemi.intensity = 0.9;
  const dir = new BABYLON.DirectionalLight("dir", new BABYLON.Vector3(-0.5, -1, -0.4), sceneRef);
  dir.position = new BABYLON.Vector3(3, 6, 4);
  dir.intensity = 0.7;

  const ground = BABYLON.MeshBuilder.CreateGround("grid", { width: 8, height: 8, subdivisions: 20 }, sceneRef);
  const groundMaterial = new BABYLON.StandardMaterial("ground-mat", sceneRef);
  groundMaterial.diffuseColor = new BABYLON.Color3(0.07, 0.14, 0.18);
  groundMaterial.emissiveColor = new BABYLON.Color3(0.05, 0.11, 0.15);
  groundMaterial.alpha = 0.55;
  groundMaterial.wireframe = true;
  ground.material = groundMaterial;
  ground.position.y = 0;

  createAxes(sceneRef, 1.1);
  return sceneRef;
}

function createAxes(sceneRef, size) {
  const axes = [
    { points: [BABYLON.Vector3.Zero(), new BABYLON.Vector3(size, 0, 0)], color: BABYLON.Color3.FromHexString("#ff6f61") },
    { points: [BABYLON.Vector3.Zero(), new BABYLON.Vector3(0, size, 0)], color: BABYLON.Color3.FromHexString("#4aa8ff") },
    { points: [BABYLON.Vector3.Zero(), new BABYLON.Vector3(0, 0, size)], color: BABYLON.Color3.FromHexString("#57d67c") }
  ];
  axes.forEach((axis, index) => {
    const lines = BABYLON.MeshBuilder.CreateLines(`axis-${index}`, { points: axis.points }, sceneRef);
    lines.color = axis.color;
  });
}

function connect() {
  if (!enable3d || !scene) {
    return;
  }
  if (modelStatus) {
    modelStatus.textContent = "Connecting pose stream...";
  }
  const ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    if (modelStatus) {
      modelStatus.textContent = "Pose stream connected";
    }
  };

  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.alignment) {
      renderAlignment(payload.alignment);
    }
    if (payload.type !== "pose_update") {
      return;
    }
    applyPoseUpdate(payload);
  };

  ws.onerror = () => {
    if (modelStatus) {
      modelStatus.textContent = "Pose stream error";
    }
  };

  ws.onclose = () => {
    if (modelStatus) {
      modelStatus.textContent = "Pose stream disconnected, retrying...";
    }
    window.setTimeout(connect, 1000);
  };
}

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

async function refreshRecordingStatus({ refreshTopics = false, force = false } = {}) {
  if (!recordingPanel) {
    return null;
  }
  if (!force && !recordingPanel.isConnected) {
    return null;
  }
  try {
    const response = await fetch(`/api/recording/status?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to fetch recording status.");
    }
    renderRecordingStatus(payload);
    if (Array.isArray(payload.recent_output) && payload.recent_output.length > 0) {
      replaceRecordingOutput(payload.recent_output.join("\n"));
    }
    if (refreshTopics) {
      await refreshRecordTopics({ resetSelection: !recordTopicsInitialized });
    }
    return payload;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordingOutput(`Recording status error: ${message}`);
    setRecordTopicStatus(message);
    return null;
  }
}

async function refreshRecordTopics({ resetSelection = false } = {}) {
  if (!recordTopicGroups) {
    return null;
  }
  setRecordTopicRefreshBusy(true);
  try {
    const response = await fetch(`/api/recording/topics?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to refresh recording topics.");
    }
    renderTopicCatalog(payload, { resetSelection });
    setRecordTopicStatus(`Topic list refreshed: ${(payload.topics || []).length} topics`);
    return payload;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordTopicStatus(message);
    setRecordingOutput(`Topic refresh error: ${message}`);
    return null;
  } finally {
    setRecordTopicRefreshBusy(false);
  }
}

function renderTopicCatalog(catalog, { resetSelection = false } = {}) {
  if (!recordTopicGroups) {
    return;
  }
  const liveTopics = Array.isArray(catalog && catalog.topics) ? catalog.topics : [];
  const defaultSelectedTopics = Array.isArray(catalog && catalog.default_selected_topics)
    ? catalog.default_selected_topics.filter((topic) => liveTopics.includes(topic))
    : [];
  const previousSelection = new Set(selectedRecordTopics);
  const previousKnown = new Set(knownRecordTopics);
  if (resetSelection || !recordTopicsInitialized || previousKnown.size === 0) {
    selectedRecordTopics = new Set(defaultSelectedTopics);
  } else {
    const mergedSelection = new Set();
    liveTopics.forEach((topic) => {
      if (previousSelection.has(topic) || !previousKnown.has(topic)) {
        mergedSelection.add(topic);
      }
    });
    selectedRecordTopics = mergedSelection;
  }
  knownRecordTopics = new Set(liveTopics);
  recordTopicsInitialized = true;

  const groups = [];
  ((catalog && catalog.cameras) || []).forEach((camera) => {
    groups.push(renderCameraTopicGroup(camera));
  });
  if (Array.isArray(catalog && catalog.other) && catalog.other.length > 0) {
    groups.push(renderCameraTopicGroup({ namespace: "Other", label: "Other", detected: true, topics: catalog.other }));
  }
  recordTopicGroups.innerHTML = groups.length > 0 ? groups.join("") : '<div class="recording-output">No live topics found yet.</div>';
  bindRecordTopicInputs();
  updateRecordTopicSummary();
}

function renderCameraTopicGroup(group) {
  const topics = Array.isArray(group && group.topics) ? group.topics : [];
  const groupKey = escapeHtml((group && (group.namespace || group.label)) || "Other");
  const groupLabel = escapeHtml((group && (group.label || group.namespace)) || "Other");
  const selectedCount = topics.filter((topic) => selectedRecordTopics.has(topic.name)).length;
  return `
    <details class="record-topic-group" open>
      <summary>
        <div class="record-topic-summary">
          <label class="record-topic-select-all">
            <input type="checkbox" data-record-group="${groupKey}" ${selectedCount > 0 ? "checked" : ""}>
            <span class="record-topic-summary-main">
              <strong>${groupLabel}</strong>
              <span class="record-topic-summary-meta">${selectedCount}/${topics.length} selected</span>
            </span>
          </label>
        </div>
      </summary>
      <div class="record-topic-list">
        ${renderTopicList(topics)}
      </div>
    </details>
  `;
}

function renderTopicList(topics) {
  return topics.map((topic) => {
    const checked = selectedRecordTopics.has(topic.name) ? "checked" : "";
    return `
      <label class="record-topic-item">
        <input type="checkbox" data-record-topic value="${escapeHtml(topic.name)}" data-record-group-name="${escapeHtml(topic.group || "")}" ${checked}>
        <span class="record-topic-copy">
          <strong>${escapeHtml(topic.short_name || topic.name)}</strong>
          <span>${escapeHtml(topic.name)}</span>
        </span>
      </label>
    `;
  }).join("");
}

function bindRecordTopicInputs() {
  if (!recordTopicGroups) {
    return;
  }
  recordTopicGroups.querySelectorAll("[data-record-topic]").forEach((input) => {
    input.addEventListener("change", (event) => {
      const topic = event.currentTarget.value;
      if (event.currentTarget.checked) {
        selectedRecordTopics.add(topic);
      } else {
        selectedRecordTopics.delete(topic);
      }
      syncRecordGroupStates();
      updateRecordTopicSummary();
    });
  });
  recordTopicGroups.querySelectorAll("[data-record-group]").forEach((input) => {
    input.addEventListener("change", (event) => {
      const group = event.currentTarget.getAttribute("data-record-group");
      const checked = Boolean(event.currentTarget.checked);
      recordTopicGroups.querySelectorAll(`[data-record-group-name="${cssEscape(group)}"]`).forEach((topicInput) => {
        topicInput.checked = checked;
        if (checked) {
          selectedRecordTopics.add(topicInput.value);
        } else {
          selectedRecordTopics.delete(topicInput.value);
        }
      });
      syncRecordGroupStates();
      updateRecordTopicSummary();
    });
  });
  syncRecordGroupStates();
}

function syncRecordGroupStates() {
  if (!recordTopicGroups) {
    return;
  }
  recordTopicGroups.querySelectorAll("[data-record-group]").forEach((input) => {
    const group = input.getAttribute("data-record-group");
    const topicInputs = Array.from(recordTopicGroups.querySelectorAll(`[data-record-group-name="${cssEscape(group)}"]`));
    const checkedCount = topicInputs.filter((item) => item.checked).length;
    input.indeterminate = checkedCount > 0 && checkedCount < topicInputs.length;
    input.checked = topicInputs.length > 0 && checkedCount === topicInputs.length;
  });
}

function updateRecordTopicSummary() {
  const selectedCount = selectedRecordTopics.size;
  const totalCount = knownRecordTopics.size;
  if (totalCount === 0) {
    setRecordTopicStatus("No live topics found. Refresh after ROS topics are available.");
    return;
  }
  setRecordTopicStatus(`${selectedCount}/${totalCount} topics selected`);
}

async function startRecording() {
  if (recordingBusy) {
    return;
  }
  await refreshRecordingStatus({ refreshTopics: false, force: true });
  const topics = collectSelectedRecordTopics();
  if (topics.length === 0) {
    const message = "Select at least one topic to record.";
    setRecordTopicStatus(message);
    setRecordingOutput(message);
    return;
  }
  const bagNameInput = document.getElementById("recording-bag-name");
  const bagName = bagNameInput ? bagNameInput.value.trim() : "";

  setRecordingBusy(true);
  try {
    const body = { topics };
    if (bagName) body.bag_name = bagName;
    const response = await fetch("/api/recording/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to start recording.");
    }
    renderRecordingStatus(payload);
    setRecordingOutput(`Recording started: ${payload.output_path || "(pending path)"}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordingOutput(`Recording start failed: ${message}`);
  } finally {
    setRecordingBusy(false);
  }
}

async function stopRecording() {
  if (recordingBusy) {
    return;
  }
  setRecordingBusy(true);
  try {
    const response = await fetch("/api/recording/stop", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to stop recording.");
    }
    renderRecordingStatus(payload);
    const syncMessage = payload && payload.sync_status && payload.sync_status.message;
    setRecordingOutput(syncMessage ? `Recording stopped. ${syncMessage}` : "Recording stopped.");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordingOutput(`Recording stop failed: ${message}`);
  } finally {
    setRecordingBusy(false);
  }
}

async function syncRecordingToHost() {
  if (recordingBusy) {
    return;
  }
  setRecordingBusy(true);
  try {
    const response = await fetch("/api/recording/sync", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to sync recording to host.");
    }
    renderRecordingStatus(payload);
    const syncMessage = payload && payload.sync_status && payload.sync_status.message;
    setRecordingOutput(syncMessage || "Recording synced to host.");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordingOutput(`Recording sync failed: ${message}`);
  } finally {
    setRecordingBusy(false);
  }
}

function collectSelectedRecordTopics() {
  if (!recordTopicGroups) {
    return [];
  }
  const topics = [];
  recordTopicGroups.querySelectorAll("[data-record-topic]").forEach((input) => {
    if (input.checked) {
      topics.push(input.value);
    }
  });
  selectedRecordTopics = new Set(topics);
  return topics;
}

function renderRecordingStatus(status) {
  const active = Boolean(status && status.recording);
  const outputPath = (status && status.output_path) || "";
  const syncStatus = status && status.sync_status;
  const hostSyncDir = (status && status.host_sync_dir) || "";
  const hostSyncSshTarget = (status && status.host_sync_ssh_target) || "";
  if (recordingStatus) {
    recordingStatus.textContent = active ? `Recording to ${outputPath}` : "Recording idle";
  }
  if (recordSyncStatus) {
    const hostTargetText = hostSyncSshTarget || hostSyncDir;
    if (syncStatus && syncStatus.message) {
      recordSyncStatus.textContent = hostTargetText
        ? `${syncStatus.message} | host: ${hostTargetText}`
        : syncStatus.message;
    } else if (hostTargetText) {
      recordSyncStatus.textContent = `Host sync ready: ${hostTargetText}`;
    } else {
      recordSyncStatus.textContent = "Host sync not configured";
    }
  }
  if (!active && outputPath && recordingOutput && recordingLogLines.length === 0) {
    setRecordingOutput(`Last output: ${outputPath}`);
  }
  if (status && status.topic_catalog && !recordTopicsInitialized) {
    renderTopicCatalog(status.topic_catalog, { resetSelection: true });
  }
  setRecordingBusy(recordingBusy, { active });
}

function setRecordingBusy(isBusy, { active } = {}) {
  recordingBusy = Boolean(isBusy);
  const isActive = typeof active === "boolean" ? active : Boolean(recordingStatus && recordingStatus.textContent.startsWith("Recording to "));
  if (startRecordingButton) {
    startRecordingButton.disabled = recordingBusy || isActive;
    startRecordingButton.classList.toggle("is-busy", recordingBusy && !isActive);
  }
  if (stopRecordingButton) {
    stopRecordingButton.disabled = recordingBusy || !isActive;
    stopRecordingButton.classList.toggle("is-busy", recordingBusy && isActive);
  }
  if (syncRecordingButton) {
    syncRecordingButton.disabled = recordingBusy || isActive;
    syncRecordingButton.classList.toggle("is-busy", recordingBusy && !isActive);
  }
}

function setRecordTopicRefreshBusy(isBusy) {
  recordTopicRefreshBusy = Boolean(isBusy);
  if (refreshRecordTopicsButton) {
    refreshRecordTopicsButton.disabled = recordTopicRefreshBusy;
    refreshRecordTopicsButton.classList.toggle("is-busy", recordTopicRefreshBusy);
  }
}

function setRecordTopicStatus(message) {
  if (recordTopicStatus) {
    recordTopicStatus.textContent = message;
  }
}

function setRecordingOutput(message) {
  if (!recordingOutput) {
    return;
  }
  const text = String(message || "").trim();
  if (!text) {
    return;
  }
  recordingLogLines.push(text);
  if (recordingLogLines.length > 12) {
    recordingLogLines = recordingLogLines.slice(-12);
  }
  recordingOutput.textContent = recordingLogLines.join("\n");
}

function replaceRecordingOutput(message) {
  if (!recordingOutput) {
    return;
  }
  const text = String(message || "").trim();
  recordingLogLines = text ? text.split("\n").slice(-12) : [];
  recordingOutput.textContent = recordingLogLines.join("\n");
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(String(value));
  }
  return String(value).replaceAll('"', '\\"');
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
    keptPoints.clear();
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
    keptPoints.clear();
    for (const trail of trailStates.values()) clearTrail(trail);
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

async function clearAllTrajectories() {
  keptPoints.clear();
  for (const trail of trailStates.values()) clearTrail(trail);
  try {
    await fetch("/api/trajectory/clear", { method: "POST" });
  } catch (_) {
    // best-effort
  }
}

async function refreshRosbags() {
  setBagListStatus("Loading bags...");
  try {
    const response = await fetch(`/api/rosbags?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to load rosbags.");
    }
    knownRosbags = Array.isArray(payload.bags) ? payload.bags : [];
    renderBagList(knownRosbags);
    renderBagSelects(knownRosbags);
    setBagListStatus(`${knownRosbags.length} bags in ${payload.rosbag_root || "rosbags"}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setBagListStatus(message);
    renderBagList([]);
    renderBagSelects([]);
  }
}

function renderBagList(bags) {
  if (!bagList) {
    return;
  }
  if (!Array.isArray(bags) || bags.length === 0) {
    bagList.innerHTML = '<div class="empty-state">No local rosbags found yet.</div>';
    return;
  }
  bagList.innerHTML = bags.map((bag) => `
    <article class="bag-row">
      <div class="bag-row-main">
        <strong>${escapeHtml(bag.name || "unnamed bag")}</strong>
        <span>${escapeHtml(bag.path || "")}</span>
      </div>
      <div class="bag-row-stats">
        <span>${formatDuration(Number(bag.duration_s || 0))}</span>
        <span>${escapeHtml(bag.size_label || "--")}</span>
        <span>${Number(bag.message_count || 0).toLocaleString()} msgs</span>
        <span>${Number(bag.topic_count || 0)} topics</span>
      </div>
      <div class="bag-badges">
        <span class="bag-badge ${bag.scored ? "is-ok" : ""}">${bag.scored ? "scored" : "unscored"}</span>
        <span class="bag-badge ${bag.optimized ? "is-ok" : ""}">${bag.optimized ? "optimized" : "not optimized"}</span>
      </div>
      <div class="bag-row-actions">
        <button type="button" class="bag-delete-button" data-bag-name="${escapeHtml(bag.name || "")}">Delete</button>
      </div>
    </article>
  `).join("");
  bagList.querySelectorAll(".bag-delete-button").forEach((btn) => {
    btn.addEventListener("click", () => deleteBag(btn.dataset.bagName));
  });
}

function renderBagSelects(bags) {
  const selects = Array.from(document.querySelectorAll("[data-bag-select]"));
  if (selects.length === 0) {
    return;
  }
  selects.forEach((select) => {
    const previous = select.value;
    if (!Array.isArray(bags) || bags.length === 0) {
      select.innerHTML = '<option value="">No local rosbags found</option>';
      updateSelectedBagMeta(select);
      return;
    }
    select.innerHTML = bags.map((bag) => `<option value="${escapeHtml(bag.name || "")}">${escapeHtml(bag.name || "")}</option>`).join("");
    if (previous && bags.some((bag) => bag.name === previous)) {
      select.value = previous;
    }
    select.onchange = () => updateSelectedBagMeta(select);
    updateSelectedBagMeta(select);
  });
}

function updateSelectedBagMeta(select) {
  const bag = knownRosbags.find((item) => item.name === select.value);
  const meta = select.id === "optimization-bag-select" ? optimizationBagMeta : scoringBagMeta;
  if (!meta) {
    return;
  }
  if (!bag) {
    meta.textContent = "No rosbag selected.";
    return;
  }
  meta.textContent = `${formatDuration(Number(bag.duration_s || 0))} · ${bag.size_label || "--"} · ${Number(bag.message_count || 0).toLocaleString()} messages · ${bag.label || ""}`;
}

async function deleteBag(bagName) {
  if (!bagName) return;
  if (!confirm(`Delete bag "${bagName}"?\n\nThis will permanently remove the bag directory and cannot be undone.`)) return;
  try {
    const response = await fetch(`/api/rosbags/${encodeURIComponent(bagName)}`, { method: "DELETE" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      alert(`Failed to delete bag: ${payload.error || response.statusText}`);
      return;
    }
    void refreshRosbags();
  } catch (err) {
    alert(`Error deleting bag: ${err.message}`);
  }
}

function setBagListStatus(message) {
  if (bagListStatus) {
    bagListStatus.textContent = message;
  }
}

async function refreshImageCapabilities() {
  if (!imageCapabilityStatus && !imageCapabilityList && !imagePipelineNotes) {
    return null;
  }
  setImageCapabilityStatus("Checking GStreamer/WebRTC capabilities...");
  if (refreshImageCapabilitiesButton) {
    refreshImageCapabilitiesButton.disabled = true;
  }
  try {
    const response = await fetch(`/api/images/capabilities?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to load image capabilities.");
    }
    renderImageCapabilities(payload);
    return payload;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setImageCapabilityStatus(message);
    return null;
  } finally {
    if (refreshImageCapabilitiesButton) {
      refreshImageCapabilitiesButton.disabled = false;
    }
  }
}

function renderImageCapabilities(payload) {
  const elements = (payload && payload.gstreamer && payload.gstreamer.elements) || {};
  const hardwareEncoder = payload && payload.hardware_encoder;
  const softwareEncoder = payload && payload.software_encoder;
  const activePath = (payload && payload.active_path) || "unknown";
  if (imageCapabilityStatus) {
    if (hardwareEncoder) {
      imageCapabilityStatus.textContent = `WebRTC hardware path ready: ${hardwareEncoder}`;
    } else if (payload && payload.webrtc_ready && softwareEncoder) {
      imageCapabilityStatus.textContent = `WebRTC transport ready · encoder fallback: ${softwareEncoder}`;
    } else {
      imageCapabilityStatus.textContent = `Preview path active · ${activePath}`;
    }
  }
  if (imageCapabilityList) {
    const rows = [
      ["WebRTC", Boolean(payload && payload.webrtc_ready), "webrtcbin + nice"],
      ["Hardware H.264", Boolean(elements.nvv4l2h264enc), "nvv4l2h264enc"],
      ["Hardware H.265", Boolean(elements.nvv4l2h265enc), "nvv4l2h265enc"],
      ["NVIDIA JPEG decode", Boolean(elements.nvjpegdec), "nvjpegdec"],
      ["NVIDIA color convert", Boolean(elements.nvvidconv), "nvvidconv"],
      ["Software fallback", Boolean(softwareEncoder), softwareEncoder || "none"]
    ];
    imageCapabilityList.innerHTML = rows.map(([label, ok, detail]) => `
      <div class="capability-row ${ok ? "is-ok" : "is-missing"}">
        <strong>${escapeHtml(label)}</strong>
        <span>${ok ? "available" : "missing"} · ${escapeHtml(detail)}</span>
      </div>
    `).join("");
  }
  if (imagePipelineNotes) {
    const notes = Array.isArray(payload && payload.notes) ? payload.notes : [];
    imagePipelineNotes.innerHTML = notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("");
  }
}

function setImageCapabilityStatus(message) {
  if (imageCapabilityStatus) {
    imageCapabilityStatus.textContent = message;
  }
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return "--";
  }
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  if (minutes <= 0) {
    return `${seconds.toFixed(1)}s`;
  }
  return `${minutes}m ${remainder}s`;
}

function startCameraPolling() {
  if (!enableCameras || !cameraDock) {
    return;
  }
  pollCameraMetadata();
  window.setInterval(pollCameraMetadata, CAMERA_POLL_INTERVAL_MS);
}

async function pollCameraMetadata() {
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

async function applyPoseUpdate(payload) {
  if (!enable3d || !scene) {
    return;
  }
  const poses = payload.poses || [];
  const poseRoleKey = poses.map((p) => p.role).join(",");
  const needsRebuild = !legend || legend.dataset.roleKey !== poseRoleKey;

  if (needsRebuild && legend) {
    const legendRows = [];
    for (const pose of poses) {
      const style = ROLE_STYLE[pose.role] || { label: pose.role, color: "#cccccc" };
      legendRows.push(
        `<div class="legend-row" data-legend-role="${escapeHtml(pose.role)}">
          <div class="legend-main">
            <span><span class="swatch" style="background:${style.color}"></span><strong>${style.label}</strong></span>
            <span class="legend-meta"></span>
          </div>
          <label class="trail-toggle">
            <span>Trail</span>
            <input type="checkbox" data-role="${escapeHtml(pose.role)}" ${isTrailEnabled(pose.role) ? "checked" : ""}>
          </label>
        </div>`
      );
    }
    legend.innerHTML = legendRows.join("");
    legend.dataset.roleKey = poseRoleKey;
    bindTrailToggles();
  }

  // Fire all poses' model loads concurrently instead of a sequential
  // `for...await`: without this, a slow first-time model fetch (e.g. the
  // ~14MB Iron Man helmet) blocked position/trail updates for the *other*
  // cameras until it finished, since the loop couldn't reach their
  // iteration until the previous await resolved.
  await Promise.all(poses.map(async (pose) => {
    const node = ensurePoseNode(pose);
    if (!node) return;
    if (!node.position) node.position = new BABYLON.Vector3(0, 0, 0);
    if (!node.rotationQuaternion) node.rotationQuaternion = new BABYLON.Quaternion(0, 0, 0, 1);
    const position = Array.isArray(pose.position) ? pose.position : [0, 0, 0];
    const quaternion = Array.isArray(pose.quaternion_xyzw) ? pose.quaternion_xyzw : [0, 0, 0, 1];
    const scenePosition = mapDashboardPositionToScene(position);
    const sceneQuaternion = mapDashboardQuaternionToScene(quaternion);
    node.setEnabled(Boolean(pose.visible));
    node.position.copyFromFloats(scenePosition.x, scenePosition.y, scenePosition.z);
    node.rotationQuaternion.copyFromFloats(sceneQuaternion.x, sceneQuaternion.y, sceneQuaternion.z, sceneQuaternion.w);
    await ensurePoseVisual(pose, node);
    applyGripperOpening(pose, node);
    updateTrailFromPose(pose);
    if (legend) {
      const row = legend.querySelector(`[data-legend-role="${CSS.escape(pose.role)}"] .legend-meta`);
      if (row) row.textContent = pose.visible ? pose.name : `${pose.name} hidden`;
    }
  }));
}

function ensurePoseNode(pose) {
  if (poseNodes.has(pose.name)) {
    return poseNodes.get(pose.name);
  }
  const node = new BABYLON.TransformNode(`pose-${pose.name}`, scene);
  node.position = new BABYLON.Vector3(0, 0, 0);
  node.rotationQuaternion = new BABYLON.Quaternion(0, 0, 0, 1);
  poseNodes.set(pose.name, node);
  return node;
}

function mapDashboardPositionToScene(sample) {
  const forward = Number(sample[0] || 0);
  const right = Number(sample[1] || 0);
  const up = Number(sample[2] || 0);
  return new BABYLON.Vector3(-right, up, forward);
}

function mapDashboardQuaternionToScene(quaternion) {
  const q = new BABYLON.Quaternion(
    Number(quaternion[0] || 0),
    Number(quaternion[1] || 0),
    Number(quaternion[2] || 0),
    Number(quaternion[3] || 1)
  );
  const dashboardToSceneBasis = BABYLON.Matrix.FromValues(
    0, -1, 0, 0,
    0, 0, 1, 0,
    1, 0, 0, 0,
    0, 0, 0, 1
  );
  const dashboardRotation = new BABYLON.Matrix();
  BABYLON.Matrix.FromQuaternionToRef(q, dashboardRotation);
  const sceneRotation = dashboardToSceneBasis.multiply(dashboardRotation).multiply(dashboardToSceneBasis.transpose());
  const sceneQuaternion = new BABYLON.Quaternion();
  BABYLON.Quaternion.FromRotationMatrixToRef(sceneRotation, sceneQuaternion);
  return sceneQuaternion;
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
    });
  for (const [name, panel] of cameraPanels.entries()) {
    if (!seen.has(name)) {
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
      <div class="camera-overlay">
        <span class="camera-fps" data-camera-fps>-- fps</span>
        <span data-camera-status>waiting</span>
      </div>
    </div>
  `;
  const img = panel.querySelector(".camera-frame");
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
  if (enableImages) {
    panel.style.gridColumn = "";
    panel.style.gridRow = "";
    return;
  }
  panel.style.gridColumn = `${index + 1} / span 1`;
  panel.style.gridRow = "1 / span 1";
}

function updateCameraStream(panel, camera) {
  const img = panel.querySelector(".camera-frame");
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
  img.src = `${camera.frame_url}?v=${version}&ts=${Date.now()}`;
}

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

async function ensurePoseVisual(pose, node) {
  if (!scene) {
    return;
  }
  if (node.metadata && node.metadata.assetKey === buildAssetKey(pose)) {
    return;
  }

  disposeNodeChildren(node);
  node.metadata = { assetKey: buildAssetKey(pose) };

  const modelPath = pose.avatar_model || "";
  const lower = modelPath.toLowerCase();
  if (!modelPath) {
    attachPrimitive(pose, node, "No model configured, using primitive fallback");
    return;
  }
  if (lower.endsWith(".obj")) {
    warnOnce(`obj:${modelPath}`, `OBJ is not used in the web dashboard: ${modelPath}. Convert it to GLB/glTF; using primitive fallback.`);
    attachPrimitive(pose, node, "OBJ unsupported, using primitive fallback");
    return;
  }
  if (!lower.endsWith(".glb") && !lower.endsWith(".gltf")) {
    warnOnce(`ext:${modelPath}`, `Unsupported model extension for ${modelPath}; using primitive fallback.`);
    attachPrimitive(pose, node, "Unsupported model extension, using primitive fallback");
    return;
  }
  if (!pose.asset_url) {
    attachPrimitive(pose, node, "Model path missing asset URL, using primitive fallback");
    return;
  }

  try {
    const key = pose.asset_url;
    const pluginExtension = modelPath.toLowerCase().endsWith(".glb") ? ".glb" : ".gltf";
    if (!modelPromises.has(key)) {
      modelPromises.set(
        key,
        BABYLON.SceneLoader.LoadAssetContainerAsync("", key, scene, null, pluginExtension)
      );
    }
    const container = await modelPromises.get(key);
    // Babylon's instantiateModelsToScene renames EVERY node (not just roots) via this
    // callback, discarding original names unless the source name is folded back in —
    // findGripperFingerNodes below depends on recovering the original glTF node names
    // (e.g. "left_finger_holder") to find the animatable finger nodes.
    const instantiated = container.instantiateModelsToScene((sourceName) => `${pose.name}-instance-${sourceName}`);
    const rootNode = new BABYLON.TransformNode(`${pose.name}-visual`, scene);
    rootNode.parent = node;
    const scaleMultiplier = (pose.role === "head" || pose.role === "left_hand" || pose.role === "right_hand") ? 0.2 : 1.0;
    const scaledSize = pose.avatar_scale * scaleMultiplier;
    rootNode.scaling = new BABYLON.Vector3(scaledSize, scaledSize, scaledSize);
    const offset = Array.isArray(pose.avatar_offset_xyz) ? pose.avatar_offset_xyz : [0, 0, 0];
    rootNode.position = mapDashboardPositionToScene(offset);
    const rotationDeg = Array.isArray(pose.avatar_rotation_deg_xyz) ? pose.avatar_rotation_deg_xyz : [0, 0, 0];
    rootNode.rotationQuaternion = BABYLON.Quaternion.FromEulerAngles(
      BABYLON.Angle.FromDegrees(Number(rotationDeg[0] || 0)).radians(),
      BABYLON.Angle.FromDegrees(Number(rotationDeg[1] || 0)).radians(),
      BABYLON.Angle.FromDegrees(Number(rotationDeg[2] || 0)).radians()
    );
    const contentNode = new BABYLON.TransformNode(`${pose.name}-content`, scene);
    contentNode.parent = rootNode;
    instantiated.rootNodes.forEach((child) => {
      child.parent = contentNode;
    });
    const meshes = collectInstantiatedMeshes(instantiated.rootNodes);
    centerModelContentOnOrigin(contentNode, meshes);
    meshes.forEach((mesh) => {
      mesh.material = createReadableModelMaterial(pose, mesh.material);
      mesh.visibility = 1.0;
      mesh.isPickable = false;
    });
    node.metadata.gripperFingers = findGripperFingerNodes(instantiated.rootNodes, pose.name);
    if (modelStatus) {
      modelStatus.textContent = `Models: loaded ${modelPath}`;
    }
  } catch (error) {
    warnOnce(`load:${modelPath}`, `Failed to load model ${modelPath}: ${String(error)}. Using primitive fallback.`);
    attachPrimitive(pose, node, "Model load failed, using primitive fallback");
  }
}

function attachPrimitive(pose, node, reason) {
  const style = ROLE_STYLE[pose.role] || ROLE_STYLE.head;
  const material = new BABYLON.StandardMaterial(`mat-${pose.name}`, scene);
  material.diffuseColor = BABYLON.Color3.FromHexString(style.color);
  material.emissiveColor = BABYLON.Color3.FromHexString(style.color).scale(0.35);
  const scale = Number(pose.avatar_scale || 1.0);

  let mesh;
  if (style.primitive === "sphere") {
    mesh = BABYLON.MeshBuilder.CreateSphere(`primitive-${pose.name}`, { diameter: 0.22 * scale }, scene);
  } else {
    mesh = BABYLON.MeshBuilder.CreateBox(`primitive-${pose.name}`, { size: 0.18 * scale }, scene);
  }
  mesh.material = material;
  mesh.parent = node;
  if (modelStatus) {
    modelStatus.textContent = `Models: ${reason}`;
  }
}

function createReadableModelMaterial(pose, originalMaterial) {
  const style = ROLE_STYLE[pose.role] || ROLE_STYLE.head;
  const roleColor = BABYLON.Color3.FromHexString(style.color);
  const skinColor = BABYLON.Color3.FromHexString(style.modelColor || "#d1a07f");
  const material = new BABYLON.PBRMaterial(`model-mat-${pose.name}-${Date.now()}`, scene);
  material.albedoColor = skinColor;
  material.metallic = 0.0;
  material.roughness = 0.72;
  material.alpha = 1.0;
  material.backFaceCulling = false;
  material.forceDepthWrite = true;
  material.transparencyMode = BABYLON.PBRMaterial.PBRMATERIAL_OPAQUE;
  material.emissiveColor = roleColor.scale(0.015);
  material.environmentIntensity = 0.35;
  if (originalMaterial && originalMaterial.bumpTexture) {
    material.bumpTexture = originalMaterial.bumpTexture;
  }
  return material;
}

function centerModelContentOnOrigin(contentNode, meshes) {
  if (!contentNode || !meshes.length) {
    return;
  }
  contentNode.computeWorldMatrix(true);
  let min = null;
  let max = null;
  meshes.forEach((mesh) => {
    mesh.computeWorldMatrix(true);
    const info = mesh.getBoundingInfo && mesh.getBoundingInfo();
    const vectors = info && info.boundingBox && info.boundingBox.vectorsWorld;
    if (!vectors) {
      return;
    }
    vectors.forEach((point) => {
      if (!min) {
        min = point.clone();
        max = point.clone();
      } else {
        min.minimizeInPlace(point);
        max.maximizeInPlace(point);
      }
    });
  });
  if (!min || !max) {
    return;
  }
  const centerWorld = BABYLON.Vector3.Center(min, max);
  const localFromWorld = contentNode.getWorldMatrix().clone().invert();
  const centerLocal = BABYLON.Vector3.TransformCoordinates(centerWorld, localFromWorld);
  contentNode.position.subtractInPlace(centerLocal);
}

const GRIPPER_FINGER_TOUCH_CLEARANCE_M = 0.012;

function findGripperFingerNodes(rootNodes, poseName) {
  let left = null;
  let right = null;
  const leftName = `${poseName}-instance-left_finger_holder`;
  const rightName = `${poseName}-instance-right_finger_holder`;
  rootNodes.forEach((root) => {
    root.getDescendants(false).concat([root]).forEach((node) => {
      if (node.name === leftName) left = node;
      if (node.name === rightName) right = node;
    });
  });
  if (!left || !right) {
    return null;
  }
  // Each finger node's rest (fully-open) local X is its distance from the
  // model's own mirror-symmetry center plane (X=0) — export_gripper_split_from_stl.py
  // centers each finger group on its own mesh centroid, not its inner (pad) face.
  // Driving local X all the way to 0 therefore over-closes: the pad's own half-
  // thickness extends past the centroid, so the two fingers interpenetrate by
  // about that much before the centroids actually meet. GRIPPER_FINGER_TOUCH_CLEARANCE_M
  // backs off the travel by that thickness so "closed" stops at first contact
  // instead of driving through it (tuned from visual feedback: 1cm/side of overshoot).
  return {
    left,
    right,
    leftRestPosition: left.position.clone(),
    rightRestPosition: right.position.clone(),
    leftMaxTravel: Math.abs(left.position.x) - GRIPPER_FINGER_TOUCH_CLEARANCE_M,
    rightMaxTravel: Math.abs(right.position.x) - GRIPPER_FINGER_TOUCH_CLEARANCE_M,
  };
}

function applyGripperOpening(pose, node) {
  const fingers = node.metadata && node.metadata.gripperFingers;
  if (!fingers) {
    return;
  }
  const opening = Number(pose.gripper_opening);
  if (!Number.isFinite(opening)) {
    return; // hold last-applied pose rather than snapping to a default
  }
  const closeFraction = 1.0 - Math.min(1, Math.max(0, opening));
  fingers.left.position.copyFrom(fingers.leftRestPosition).addInPlaceFromFloats(closeFraction * fingers.leftMaxTravel, 0, 0);
  fingers.right.position.copyFrom(fingers.rightRestPosition).addInPlaceFromFloats(-closeFraction * fingers.rightMaxTravel, 0, 0);
}

function collectInstantiatedMeshes(rootNodes) {
  const meshes = [];
  rootNodes.forEach((node) => {
    if (node instanceof BABYLON.AbstractMesh) {
      meshes.push(node);
    }
    node.getChildMeshes(false).forEach((mesh) => {
      meshes.push(mesh);
    });
  });
  return meshes;
}

function disposeNodeChildren(node) {
  const descendants = node.getDescendants(false);
  descendants.forEach((child) => {
    if (child.dispose) {
      child.dispose(false, true);
    }
  });
}

function buildAssetKey(pose) {
  const rotation = Array.isArray(pose.avatar_rotation_deg_xyz) ? pose.avatar_rotation_deg_xyz.join(",") : "0,0,0";
  const offset = Array.isArray(pose.avatar_offset_xyz) ? pose.avatar_offset_xyz.join(",") : "0,0,0";
  return String(pose.avatar_model || "primitive") + ":" + String(pose.avatar_scale || 1) + ":" + rotation + ":" + offset;
}

function warnOnce(key, message) {
  if (modelWarnings.has(key)) {
    return;
  }
  modelWarnings.add(key);
  console.warn(message);
}

function bindTrailToggles() {
  if (!legend) {
    return;
  }
  const inputs = legend.querySelectorAll('input[data-role]');
  inputs.forEach((input) => {
    input.addEventListener("change", (event) => {
      const role = event.currentTarget.getAttribute("data-role");
      setTrailEnabled(role, event.currentTarget.checked);
    });
  });
}

function updateTrails() {
  for (const trail of trailStates.values()) {
    if (!trail.enabled) {
      clearTrail(trail);
    }
  }
}

function ensureTrailState(role) {
  if (trailStates.has(role)) {
    return trailStates.get(role);
  }
  const state = {
    role,
    enabled: DEFAULT_TRAIL_ENABLED[role] !== false,
    points: [],
    mesh: null
  };
  trailStates.set(role, state);
  return state;
}

function setTrailEnabled(role, enabled) {
  const trail = ensureTrailState(role);
  trail.enabled = Boolean(enabled);
  if (!trail.enabled) {
    clearTrail(trail);
  }
}

function isTrailEnabled(role) {
  return ensureTrailState(role).enabled;
}

function clearTrail(trail) {
  trail.points = [];
  trail._meshPointCount = 0;
  if (trail.mesh) {
    trail.mesh.dispose(false, true);
    trail.mesh = null;
  }
}

function updateTrailFromPose(pose) {
  const trail = ensureTrailState(pose.role);
  if (!trail.enabled) {
    clearTrail(trail);
    keptPoints.delete(pose.role);
    return;
  }

  if (keepTrajectory) {
    if (pose.visible && pose.position) {
      const newPoint = mapDashboardPositionToScene(pose.position);
      const kept = keptPoints.get(pose.role) || [];
      const last = kept[kept.length - 1];
      if (!last || BABYLON.Vector3.Distance(newPoint, last) > 0.001) {
        kept.push(newPoint);
        keptPoints.set(pose.role, kept);
      }
    }
    const kept = keptPoints.get(pose.role) || [];
    if (kept.length >= 2) {
      const firstPoint = kept[0];
      const hasMotion = kept.some((point) => BABYLON.Vector3.Distance(point, firstPoint) > 0.02);
      if (hasMotion) {
        trail.points = kept.map((p) => p.clone());
        refreshTrailMesh(trail);
        return;
      }
    }
    clearTrail(trail);
    return;
  }

  if (!pose.visible) {
    clearTrail(trail);
    return;
  }
  const sourcePoints = (pose.trace || []).map((sample) => mapDashboardPositionToScene(sample));
  if (sourcePoints.length < 2) {
    clearTrail(trail);
    return;
  }
  const firstPoint = sourcePoints[0];
  const hasMotion = sourcePoints.some((point) => BABYLON.Vector3.Distance(point, firstPoint) > 0.02);
  if (!hasMotion) {
    clearTrail(trail);
    return;
  }
  trail.points = sourcePoints;
  refreshTrailMesh(trail);
}

function refreshTrailMesh(trail) {
  if (!scene) {
    return;
  }
  if (trail.points.length < 2) {
    if (trail.mesh) {
      trail.mesh.dispose(false, true);
      trail.mesh = null;
    }
    return;
  }

  const roleColor = BABYLON.Color3.FromHexString((ROLE_STYLE[trail.role] || ROLE_STYLE.head).color);
  const points = trail.points.map((point) => point.clone());
  const radius = TRAIL_RADIUS_BY_ROLE[trail.role] || 0.016;
  // Babylon.js tube instance update requires identical path length; dispose and recreate on change.
  if (trail.mesh && trail._meshPointCount !== points.length) {
    trail.mesh.dispose(false, true);
    trail.mesh = null;
  }
  if (trail.mesh) {
    BABYLON.MeshBuilder.CreateTube(
      null,
      { path: points, radius, tessellation: 10, instance: trail.mesh, updatable: true },
      scene
    );
  } else {
    trail.mesh = BABYLON.MeshBuilder.CreateTube(
      `trail-${trail.role}`,
      { path: points, radius, tessellation: 10, updatable: true },
      scene
    );
    trail.mesh.isPickable = false;
    trail.mesh.alwaysSelectAsActiveMesh = true;
    trail.mesh.renderingGroupId = 1;
  }
  trail._meshPointCount = points.length;
  if (!trail.mesh.material) {
    const material = new BABYLON.StandardMaterial(`trail-mat-${trail.role}`, scene);
    material.disableLighting = true;
    material.emissiveColor = roleColor;
    material.diffuseColor = roleColor;
    material.specularColor = BABYLON.Color3.Black();
    trail.mesh.material = material;
  }
  trail.mesh.material.alpha = 0.96;
}

async function runScoring() {
  if (scoringBusy) {
    return;
  }
  const bagSelect = document.getElementById("scoring-bag-select");
  const bagName = bagSelect ? bagSelect.value : "";
  if (!bagName) {
    setScoringStatus("Select a rosbag first.");
    return;
  }
  const topic = scoringTopicInput ? scoringTopicInput.value.trim() : "";
  const refCovRaw = scoringRefCovInput ? scoringRefCovInput.value.trim() : "";
  const refCov = refCovRaw ? parseFloat(refCovRaw) : undefined;

  scoringBusy = true;
  if (runScoringButton) {
    runScoringButton.disabled = true;
  }
  hideScoringResult();
  setScoringStatus("Starting...");

  try {
    const body = { bag_name: bagName };
    if (topic) {
      body.topic = topic;
    }
    if (refCov !== undefined && !isNaN(refCov)) {
      body.ref_cov = refCov;
    }
    const response = await fetch("/api/scoring/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) {
      setScoringStatus(`Error: ${payload.error || "Failed to start scoring."}`);
      scoringBusy = false;
      if (runScoringButton) {
        runScoringButton.disabled = false;
      }
      return;
    }
    setScoringStatus("Running... (this may take a minute)");
    scheduleScoringPoll(1500);
  } catch (error) {
    setScoringStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
    scoringBusy = false;
    if (runScoringButton) {
      runScoringButton.disabled = false;
    }
  }
}

async function pollScoringStatus() {
  try {
    const response = await fetch(`/api/scoring/status?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      return;
    }
    const status = payload.status;
    if (status === "running") {
      scoringBusy = true;
      if (runScoringButton) {
        runScoringButton.disabled = true;
      }
      const topic = payload.topic ? ` — ${payload.topic}` : "";
      setScoringStatus(`Scoring${topic}`);
      scheduleScoringPoll(1500);
    } else if (status === "done") {
      scoringBusy = false;
      if (runScoringButton) {
        runScoringButton.disabled = false;
      }
      setScoringStatus(`Scored: ${payload.bag_name || ""}`);
      renderScoringResult(payload.result);
      void refreshRosbags();
    } else if (status === "error") {
      scoringBusy = false;
      if (runScoringButton) {
        runScoringButton.disabled = false;
      }
      setScoringStatus(`Error: ${payload.error || "Unknown error"}`);
    }
  } catch (_err) {
    // Silently ignore transient polling failures.
  }
}

function scheduleScoringPoll(delayMs) {
  if (scoringPollTimer !== null) {
    clearTimeout(scoringPollTimer);
  }
  scoringPollTimer = window.setTimeout(() => {
    scoringPollTimer = null;
    void pollScoringStatus();
  }, delayMs);
}

function setScoringStatus(message) {
  if (scoringStatusEyebrow) {
    scoringStatusEyebrow.hidden = !message;
  }
  if (scoringStatusEl) {
    scoringStatusEl.hidden = !message;
    scoringStatusEl.textContent = message;
  }
}

function hideScoringResult() {
  if (scoringResultEl) {
    scoringResultEl.hidden = true;
  }
}

function scoringColor(score) {
  return score >= 90 ? "#57d67c" : score >= 70 ? "#4aa8ff" : score >= 50 ? "#f0c040" : "#ff5a5a";
}

function renderScoringCameraCard(cam) {
  if (cam.error) {
    return `
      <div style="padding:12px 16px;border-radius:8px;background:var(--panel);border:1px solid var(--line)">
        <div style="font-family:monospace;font-size:0.78rem;color:var(--muted);margin-bottom:6px">${escapeHtml(cam.topic || "")}</div>
        <span style="color:#ff5a5a;font-size:0.85rem">Error: ${escapeHtml(cam.error)}</span>
      </div>`;
  }
  const color = scoringColor(cam.score || 0);
  return `
    <div style="padding:12px 16px;border-radius:8px;background:var(--panel);border:1px solid var(--line)">
      <div style="font-family:monospace;font-size:0.78rem;color:var(--muted);margin-bottom:8px">${escapeHtml(cam.topic || "")}</div>
      <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px">
        <strong style="font-size:1.7rem;color:${escapeHtml(color)}">${escapeHtml(String(cam.score))} / 100</strong>
        <span style="font-size:0.95rem;color:var(--muted)">${escapeHtml(cam.quality || "")}</span>
      </div>
      <table style="border-collapse:collapse;width:100%;font-size:0.82rem">
        <tbody>
          <tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">Mean trace</td><td>${escapeHtml((cam.mean_trace || 0).toExponential(3))}</td></tr>
          <tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">Max trace</td><td>${escapeHtml((cam.max_trace || 0).toExponential(3))}</td></tr>
          <tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">p99 trace</td><td>${escapeHtml((cam.p99_trace || 0).toExponential(3))}</td></tr>
        </tbody>
      </table>
    </div>`;
}

function renderScoringResult(result) {
  if (!scoringResultEl || !scoringResultBody || !result) {
    return;
  }
  // multi-camera result: {cameras: [...]}
  if (result.cameras && Array.isArray(result.cameras)) {
    scoringResultBody.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
        ${result.cameras.map(renderScoringCameraCard).join("")}
      </div>`;
  } else {
    // legacy single-topic result
    const color = scoringColor(result.score || 0);
    scoringResultBody.innerHTML = `
      <div class="bag-row-main" style="margin-bottom:0.75rem">
        <strong style="font-size:2rem;color:${escapeHtml(color)}">${escapeHtml(String(result.score))} / 100</strong>
        <span style="font-size:1.1rem">${escapeHtml(result.quality || "")}</span>
      </div>
      <table style="border-collapse:collapse;width:100%;font-size:0.85rem">
        <tbody>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">Topic</td><td style="font-family:monospace;font-size:0.8rem">${escapeHtml(result.topic || "")}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">Mean cov trace</td><td>${escapeHtml((result.mean_trace || 0).toExponential(4))}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">Max cov trace</td><td>${escapeHtml((result.max_trace || 0).toExponential(4))}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">p90 cov trace</td><td>${escapeHtml((result.p90_trace || 0).toExponential(4))}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">p99 cov trace</td><td>${escapeHtml((result.p99_trace || 0).toExponential(4))}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">Reference cov</td><td>${escapeHtml((result.ref_cov || 0).toExponential(4))}</td></tr>
        </tbody>
      </table>`;
  }
  scoringResultEl.hidden = false;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

// ── Optimization page ──────────────────────────────────────────────────────

function updateAutoRunName() {
  if (!optimizationRunNameInput) return;
  if (optimizationRunNameInput.value !== "" && optimizationRunNameInput.dataset.autoFilled !== "1") return;
  const bagSel = document.getElementById("optimization-bag-select");
  const bagName = bagSel ? bagSel.value : "";
  const cameraRadio = document.querySelector('input[name="opt-camera"]:checked');
  const cameraName = cameraRadio ? cameraRadio.value : "";
  const name = bagName && cameraName ? `${bagName}_${cameraName}` : bagName || "";
  optimizationRunNameInput.value = name;
  optimizationRunNameInput.dataset.autoFilled = "1";
}

async function populateOptimizationCameras(cameras) {
  const group = optimizationCameraSelect;
  if (!group) return;
  if (!cameras || cameras.length === 0) {
    try {
      const res = await fetch(`/api/cameras?ts=${Date.now()}`, { cache: "no-store" });
      const payload = await res.json();
      cameras = Array.isArray(payload.cameras) ? payload.cameras : [];
    } catch (_) {
      return;
    }
  }
  const prevRadio = group.querySelector('input[type="radio"]:checked');
  const prev = prevRadio ? prevRadio.value : "";
  group.innerHTML = cameras.map((c, i) => {
    const id = `opt-camera-${i}`;
    const checked = (prev ? c.name === prev : i === 0) ? "checked" : "";
    return `<label class="opt-radio-item"><input type="radio" id="${escapeHtml(id)}" name="opt-camera" value="${escapeHtml(c.name || "")}" ${checked}><span>${escapeHtml(c.label || c.name || "")}</span></label>`;
  }).join("");
  group.querySelectorAll('input[type="radio"]').forEach((input) => {
    input.addEventListener("change", updateAutoRunName);
  });
  updateAutoRunName();
}

async function startOptimization() {
  if (optimizationBusy) return;
  const bagSel = document.getElementById("optimization-bag-select");
  const bagName = bagSel ? bagSel.value : "";
  if (!bagName) {
    if (optimizationStepLabel) optimizationStepLabel.textContent = "Select a rosbag first.";
    return;
  }
  const cameraRadio = document.querySelector('input[name="opt-camera"]:checked');
  const cameraName = cameraRadio ? cameraRadio.value : "";
  const streamRadio = document.querySelector('input[name="opt-stream"]:checked');
  const streamType = streamRadio ? streamRadio.value : "color_compressed";
  const runName = (optimizationRunNameInput && optimizationRunNameInput.value.trim()) || bagName;
  optimizationBusy = true;
  if (startOptimizationButton) startOptimizationButton.disabled = true;
  if (stopOptimizationButton) stopOptimizationButton.hidden = false;
  if (optimizationResultPanel) optimizationResultPanel.hidden = true;
  renderOptimizationProgress({ state: "running", step: 0, step_name: "Starting...", log_tail: [] });
  try {
    const res = await fetch("/api/optimization/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bag_name: bagName, camera_name: cameraName, stream_type: streamType, run_name: runName }),
    });
    const payload = await res.json();
    if (!res.ok) {
      renderOptimizationProgress({ state: "error", step: 0, step_name: `Error: ${payload.error || "Failed"}`, log_tail: [] });
      optimizationBusy = false;
      if (startOptimizationButton) startOptimizationButton.disabled = false;
      if (stopOptimizationButton) stopOptimizationButton.hidden = true;
      return;
    }
    if (optimizationStepLabel && payload.image_topic) {
      optimizationStepLabel.textContent = `Started — camera: ${payload.camera || cameraName}  topic: ${payload.image_topic}`;
    }
    clearInterval(optimizationPollTimer);
    optimizationPollTimer = window.setInterval(() => { void refreshOptimizationStatus(); }, 2000);
  } catch (err) {
    renderOptimizationProgress({ state: "error", step: 0, step_name: `Error: ${err instanceof Error ? err.message : String(err)}`, log_tail: [] });
    optimizationBusy = false;
    if (startOptimizationButton) startOptimizationButton.disabled = false;
    if (stopOptimizationButton) stopOptimizationButton.hidden = true;
  }
}

async function stopOptimization() {
  clearInterval(optimizationPollTimer);
  optimizationPollTimer = null;
  try {
    await fetch("/api/optimization/stop", { method: "POST" });
  } catch (_) {}
  optimizationBusy = false;
  if (startOptimizationButton) startOptimizationButton.disabled = false;
  if (stopOptimizationButton) stopOptimizationButton.hidden = true;
  renderOptimizationProgress({ state: "idle", step: 0, step_name: "Stopped.", log_tail: [] });
}

async function refreshOptimizationStatus() {
  try {
    const res = await fetch(`/api/optimization/status?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await res.json();
    if (!res.ok) return;
    renderOptimizationProgress(payload);
    if (payload.state === "done") {
      clearInterval(optimizationPollTimer);
      optimizationPollTimer = null;
      optimizationBusy = false;
      if (startOptimizationButton) startOptimizationButton.disabled = false;
      if (stopOptimizationButton) stopOptimizationButton.hidden = true;
      void renderOptimizationResult(payload.result, payload.run_name || "");
      void refreshRosbags();
      void populateOptimizationRuns();
    } else if (payload.state === "error") {
      clearInterval(optimizationPollTimer);
      optimizationPollTimer = null;
      optimizationBusy = false;
      if (startOptimizationButton) startOptimizationButton.disabled = false;
      if (stopOptimizationButton) stopOptimizationButton.hidden = true;
    }
  } catch (_) {}
}

function renderOptimizationProgress(payload) {
  const state = (payload && payload.state) || "idle";
  const step = Number(payload && payload.step) || 0;
  const subProgress = (payload && typeof payload.sub_progress === "number") ? payload.sub_progress : 0;
  const stepName = (payload && payload.step_name) || "";
  const logLines = (payload && Array.isArray(payload.log_tail)) ? payload.log_tail : [];

  // Per-step percentage ranges: [start%, end%]
  // Steps 1-4 occupy these bands; step 3 (COLMAP) gets the big middle.
  const STEP_RANGES = [
    [0,  0],   // unused (step 0)
    [1,  7],   // step 1 — VIO extraction
    [7, 15],   // step 2 — image extraction
    [15, 90],  // step 3 — COLMAP (sub_progress gives fine detail)
    [90, 98],  // step 4 — Sim3 alignment
  ];

  let pct = 0;
  if (state === "done") {
    pct = 100;
  } else if (state === "running") {
    if (step === 0) {
      pct = 1;
    } else {
      const [lo, hi] = STEP_RANGES[step] || [0, 100];
      const frac = (step === 3) ? subProgress : 0.5;
      pct = Math.round(lo + frac * (hi - lo));
    }
  } else if (state === "error") {
    const [lo] = (step > 0 ? STEP_RANGES[step] : [0]) || [0];
    pct = lo;
  }

  const fill = document.getElementById("optimization-progress-fill");
  if (fill) {
    fill.style.width = `${pct}%`;
    fill.classList.toggle("is-error", state === "error");
  }
  const percentLabel = document.getElementById("optimization-percent-label");
  if (percentLabel) percentLabel.textContent = state === "idle" ? "" : `${pct}%`;

  if (optimizationStepLabel) {
    if (state === "idle") optimizationStepLabel.textContent = "Idle";
    else if (state === "done") optimizationStepLabel.textContent = "Complete";
    else if (state === "error") optimizationStepLabel.textContent = `Error — ${stepName || `step ${step}`}`;
    else optimizationStepLabel.textContent = step > 0 ? `Step ${step}/4 — ${stepName}` : "Starting...";
  }

  if (optimizationLogEl && logLines.length > 0) {
    const atBottom = optimizationLogEl.scrollHeight - optimizationLogEl.scrollTop - optimizationLogEl.clientHeight < 40;
    optimizationLogEl.textContent = logLines.join("\n");
    if (atBottom) optimizationLogEl.scrollTop = optimizationLogEl.scrollHeight;
  }
}

async function renderOptimizationResult(result, runName) {
  if (!optimizationResultPanel) return;
  if (result && optimizationLogLink && result.colmap_log) {
    optimizationLogLink.href = result.colmap_log;
  }
  optimizationResultPanel.hidden = false;

  const name = runName ||
    (optimizationRunNameInput && optimizationRunNameInput.value.trim()) ||
    (document.getElementById("optimization-bag-select") || {}).value || "";
  if (!name) return;

  try {
    const res = await fetch(`/api/optimization/trajectories?run_name=${encodeURIComponent(name)}`, { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) return;
    buildOptTrajScene(data.vio || [], data.colmap || [], name);
  } catch (_) {}
}

async function populateOptimizationRuns() {
  const select = document.getElementById("opt-saved-run-select");
  if (!select) return;
  try {
    const res = await fetch("/api/optimization/runs", { cache: "no-store" });
    const data = await res.json();
    const runs = data.runs || [];
    select.innerHTML = runs.length
      ? runs.map(r => `<option value="${escapeHtml(r.run_name)}">${escapeHtml(r.run_name)}${r.has_sim3 ? "" : " (no Sim3)"}</option>`).join("")
      : `<option value="">No saved runs</option>`;
  } catch (_) {
    select.innerHTML = `<option value="">Error loading runs</option>`;
  }
}

async function loadSavedOptRun() {
  const select = document.getElementById("opt-saved-run-select");
  const runName = select ? select.value : "";
  if (!runName) return;
  await renderOptimizationResult(null, runName);
}

let _optEngine = null;
let _optRenderedRun = null;

function buildOptTrajScene(vioPoints, colmapPoints, runName) {
  if (runName && runName === _optRenderedRun) return;
  _optRenderedRun = runName || null;
  const canvas = document.getElementById("opt-traj-canvas");
  if (!canvas || typeof BABYLON === "undefined") return;

  if (_optEngine) {
    _optEngine.dispose();
    _optEngine = null;
  }

  canvas.width = canvas.offsetWidth;
  canvas.height = canvas.offsetHeight;

  const engine = new BABYLON.Engine(canvas, true, { preserveDrawingBuffer: false, stencil: false });
  _optEngine = engine;
  const scene = new BABYLON.Scene(engine);
  scene.clearColor = new BABYLON.Color4(0.05, 0.07, 0.05, 1);

  const camera = new BABYLON.ArcRotateCamera("cam", -Math.PI / 2, Math.PI / 3, 3, BABYLON.Vector3.Zero(), scene);
  camera.attachControl(canvas, true);
  camera.lowerRadiusLimit = 0.1;
  camera.wheelPrecision = 50;

  new BABYLON.HemisphericLight("light", new BABYLON.Vector3(0, 1, 0), scene);

  function toVec3(p) { return new BABYLON.Vector3(p[0], p[2], p[1]); }

  function centroid(pts) {
    if (!pts.length) return [0, 0, 0];
    let sx = 0, sy = 0, sz = 0;
    for (const p of pts) { sx += p[0]; sy += p[1]; sz += p[2]; }
    return [sx / pts.length, sy / pts.length, sz / pts.length];
  }

  const allPts = [...vioPoints, ...colmapPoints];
  const c = centroid(allPts);

  function normPts(pts) { return pts.map((p) => [p[0] - c[0], p[1] - c[1], p[2] - c[2]]); }

  const vioNorm = normPts(vioPoints);
  const colmapNorm = normPts(colmapPoints);

  function drawLine(pts, color) {
    if (pts.length < 2) return;
    const points = pts.map((p) => new BABYLON.Vector3(p[0], p[2], p[1]));
    const lines = BABYLON.MeshBuilder.CreateLines("line", { points, updatable: false }, scene);
    lines.color = color;
  }

  drawLine(vioNorm, new BABYLON.Color3(1, 0.35, 0.35));
  drawLine(colmapNorm, new BABYLON.Color3(0.34, 0.84, 0.48));

  const span = allPts.length > 0 ? Math.max(...allPts.map((p) => Math.abs(p[0] - c[0])), ...allPts.map((p) => Math.abs(p[2] - c[2]))) : 1;
  camera.radius = Math.max(span * 2.5, 0.5);

  engine.runRenderLoop(() => scene.render());
  window.addEventListener("resize", () => {
    canvas.width = canvas.offsetWidth;
    canvas.height = canvas.offsetHeight;
    engine.resize();
  });
}
