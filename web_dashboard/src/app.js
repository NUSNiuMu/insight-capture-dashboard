const dashboardView = document.body.dataset.dashboardView || "full";

const canvas = document.getElementById("render-canvas");
const enable3d = Boolean(canvas);
const modelStatus = document.getElementById("model-status");
const mappingVersion = document.getElementById("mapping-version");
const legend = document.getElementById("pose-legend");
const trailWidthSlider = document.getElementById("trail-width-slider");
const trailWidthValue = document.getElementById("trail-width-value");
const trailKeepAllToggle = document.getElementById("trail-keep-all-toggle");
const trailClearButton = document.getElementById("trail-clear-button");
const trailStatus = document.getElementById("trail-status");
const cameraDock = document.getElementById("camera-dock");
const enableCameras = Boolean(cameraDock);
const cameraPageMeta = document.getElementById("camera-page-meta");
const refreshBagsButton = document.getElementById("refresh-bags-button");
const imageSourceSelect = document.getElementById("image-source-select");
const imageLiveButton = document.getElementById("image-live-button");
const imagePlayButton = document.getElementById("image-play-button");
const imageStopButton = document.getElementById("image-stop-button");
const imagePlaybackStatus = document.getElementById("image-playback-status");
const bagList = document.getElementById("bag-list");
const bagRoot = document.getElementById("bag-root");
const dashboardStatus = document.getElementById("dashboard-status");
const poseSummary = document.getElementById("pose-summary");
const imageSummary = document.getElementById("image-summary");
const bagSummary = document.getElementById("bag-summary");
const jobSummary = document.getElementById("job-summary");
const recordingStatus = document.getElementById("recording-status");
const startRecordingButton = document.getElementById("start-recording-button");
const stopRecordingButton = document.getElementById("stop-recording-button");
const alignmentStartButton = document.getElementById("alignment-start-button");
const alignmentStopButton = document.getElementById("alignment-stop-button");
const alignmentLogOutput = document.getElementById("alignment-log-output");
const postprocessOutput = document.getElementById("postprocess-output");
const workflowOutputs = Array.from(document.querySelectorAll("[data-workflow-output]"));
const monitorDashboardButtons = Array.from(document.querySelectorAll("[data-monitor-dashboard-launch]"));
const monitorDashboardStatuses = Array.from(document.querySelectorAll("[data-monitor-dashboard-status]"));
const actionBagSelects = Array.from(document.querySelectorAll("[data-action-bag]"));
const recordTopicGroups = document.getElementById("record-topic-groups");
const refreshRecordTopicsButton = document.getElementById("refresh-record-topics-button");
const recordTopicStatus = document.getElementById("record-topic-status");
const alignmentStartHint = document.getElementById("alignment-start-hint");

const ROLE_STYLE = {
  head: { label: "Head", color: "#57d67c", primitive: "sphere", modelColor: "#d6a07d" },
  left_hand: { label: "Left Hand", color: "#4aa8ff", primitive: "box", modelColor: "#c98d6b" },
  right_hand: { label: "Right Hand", color: "#ff6f61", primitive: "box", modelColor: "#c98d6b" }
};
const TRAIL_RADIUS_BY_ROLE = {
  head: 0.01,
  left_hand: 0.008,
  right_hand: 0.008
};
const GRIPPER_SLIDER_TRAVEL_METERS = 0.12;
const UMI_ARTICULATED_MODEL_PATH = "assets/models/UMI_Gripper_articulated.glb";
const UMI_SPLIT_ASSETS = {
  static: "assets/models/UMI_Gripper_static.glb",
  leftFinger: "assets/models/UMI_Gripper_left_finger.glb",
  rightFinger: "assets/models/UMI_Gripper_right_finger.glb"
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

const CAMERA_FPS_WINDOW_MS = 1500;
const CAMERA_METADATA_INTERVAL_MS = 2000;
const DEFAULT_TRAIL_ENABLED = {
  head: true,
  left_hand: true,
  right_hand: true
};
const TRAIL_WIDTH_STORAGE_KEY = "insight-trail-width-multiplier";
let trailWidthMultiplier = loadTrailWidthMultiplier();
const SCENE_MAPPING_VERSION = "RUF-v3";
let manualGripperOpenRatio = null;
let selectedBagName = "";
let knownBags = [];
let selectedRecordTopics = new Set();
let knownRecordTopics = new Set();
let recordTopicsInitialized = false;
let latestLegendPoses = [];
let currentLegendSignature = "";
let currentView = "main";
let monitorDashboardRunning = false;
let latestAlignmentStatus = null;
let keepAllTrailPoints = false;
let trailMaxPoints = 300;
let ignoreTrailUpdatesUntilMs = 0;
let currentTraceGeneration = null;
let currentPoseSource = null;

initializeNavigation();
if (engine && scene) {
  engine.runRenderLoop(() => {
    if (manualGripperOpenRatio !== null) {
      refreshAllArticulations();
    }
    updateTrails();
    scene.render();
  });

  window.addEventListener("resize", () => engine.resize());
}

if (enable3d) {
  if (mappingVersion) {
    mappingVersion.textContent = `Mapping: ${SCENE_MAPPING_VERSION} [x=right y=up z=-forward]`;
  }
  initializeTrailWidthControls();
  initializeTrailRetentionControls();
  initializeGripperKeyboardControls();
  connect();
}
if (enableCameras) {
  startCameraPolling();
}
initializePostProcessingPanel();
initializeMonitorDashboardLauncher();

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

function initializeNavigation() {
  const buttons = Array.from(document.querySelectorAll("[data-view-link]"));
  const views = Array.from(document.querySelectorAll("[data-view]"));
  if (!buttons.length || !views.length) {
    return;
  }
  const showView = (viewName) => {
    currentView = viewName;
    views.forEach((view) => view.classList.toggle("active", view.dataset.view === viewName));
    buttons.forEach((button) => button.classList.toggle("active", button.dataset.viewLink === viewName));
    if (window.location.hash !== `#${viewName}`) {
      window.location.hash = viewName;
    }
    if (engine) {
      window.setTimeout(() => engine.resize(), 50);
    }
    if (viewName === "main" || viewName === "images") {
      pollCameraMetadata();
    }
    if (viewName === "recording") {
      refreshRecordingStatus({ refreshTopics: true });
    }
    if (viewName === "alignment") {
      refreshAlignmentStatus();
    }
  };
  buttons.forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewLink)));
  const initialHash = window.location.hash.replace("#", "");
  const initial = initialHash === "postprocess" ? "recording" : (initialHash || "main");
  showView(views.some((view) => view.dataset.view === initial) ? initial : "main");
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
    if (dashboardStatus) {
      dashboardStatus.textContent = "Pose stream connected";
    }
  };

  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type !== "pose_update") {
      return;
    }
    if (poseSummary) {
      const visible = (payload.poses || []).filter((pose) => pose.visible).length;
      poseSummary.textContent = `${visible}/${(payload.poses || []).length} poses visible`;
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

function startCameraPolling() {
  if (!enableCameras || !cameraDock) {
    return;
  }
  pollCameraMetadata();
  window.setInterval(pollCameraMetadata, CAMERA_METADATA_INTERVAL_MS);
}

async function pollCameraMetadata() {
  if (currentView !== "main" && currentView !== "images") {
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
    renderCameraPanels(payload.cameras || []);
    if (cameraPageMeta) {
      const liveCount = (payload.cameras || []).filter((camera) => !camera.stale && camera.visible).length;
      cameraPageMeta.textContent = `${liveCount}/${(payload.cameras || []).length} streams live`;
    }
    if (imageSummary) {
      const liveCount = (payload.cameras || []).filter((camera) => !camera.stale && camera.visible).length;
      imageSummary.textContent = `${liveCount}/${(payload.cameras || []).length} streams live`;
    }
  } catch (_error) {
    // The pose WebSocket remains the primary status signal; image polling can retry quietly.
  }
}

async function applyPoseUpdate(payload) {
  if (!enable3d || !scene) {
    return;
  }
  applyPoseSource(payload.source || "live");
  applyTraceGeneration(payload.trace_generation);
  const poses = payload.poses || [];
  latestLegendPoses = poses;
  renderPoseLegend(poses);
  for (const pose of poses) {
    const node = ensurePoseNode(pose);
    if (!node) {
      continue;
    }
    node.metadata = { ...(node.metadata || {}), poseRole: pose.role };
    if (!node.position) {
      node.position = new BABYLON.Vector3(0, 0, 0);
    }
    if (!node.rotationQuaternion) {
      node.rotationQuaternion = new BABYLON.Quaternion(0, 0, 0, 1);
    }
    const position = Array.isArray(pose.position) ? pose.position : [0, 0, 0];
    const quaternion = Array.isArray(pose.quaternion_xyzw) ? pose.quaternion_xyzw : [0, 0, 0, 1];
    const scenePosition = mapDashboardPositionToScene(position);
    const sceneQuaternion = mapDashboardQuaternionToScene(quaternion);
    node.setEnabled(Boolean(pose.visible));
    node.position.copyFromFloats(
      scenePosition.x,
      scenePosition.y,
      scenePosition.z
    );
    node.rotationQuaternion.copyFromFloats(
      sceneQuaternion.x,
      sceneQuaternion.y,
      sceneQuaternion.z,
      sceneQuaternion.w
    );
    await ensurePoseVisual(pose, node);
    applyPoseArticulation(pose, node);
    updateTrailFromPose(pose);

  }
}

function renderPoseLegend(poses) {
  if (!legend) {
    return;
  }
  const signature = (poses || [])
    .map((pose) => `${pose.role}:${pose.name}:${pose.visible ? 1 : 0}:${isTrailEnabled(trailKeyForPose(pose), pose.role) ? 1 : 0}`)
    .join("|");
  if (signature === currentLegendSignature) {
    return;
  }
  currentLegendSignature = signature;
  legend.innerHTML = (poses || []).map((pose) => {
    const style = ROLE_STYLE[pose.role] || { label: pose.role, color: "#cccccc" };
    const trailKey = trailKeyForPose(pose);
    return `
      <div class="legend-row">
        <div class="legend-main">
          <span><span class="swatch" style="background:${style.color}"></span><strong>${style.label}</strong></span>
          <span class="legend-meta">${pose.visible ? pose.name : pose.name + " hidden"}</span>
        </div>
        <label class="trail-toggle">
          <span>Trail</span>
          <input type="checkbox" data-trail-key="${escapeHtml(trailKey)}" data-role="${escapeHtml(pose.role)}" ${isTrailEnabled(trailKey, pose.role) ? "checked" : ""}>
        </label>
      </div>`;
  }).join("");
  bindTrailToggles();
}

function trailKeyForPose(pose) {
  return pose.name || pose.role;
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
  return new BABYLON.Vector3(right, up, -forward);
}

function mapDashboardQuaternionToScene(quaternion) {
  const q = new BABYLON.Quaternion(
    Number(quaternion[0] || 0),
    Number(quaternion[1] || 0),
    Number(quaternion[2] || 0),
    Number(quaternion[3] || 1)
  );
  const dashboardToSceneBasis = BABYLON.Matrix.FromValues(
    0, 1, 0, 0,
    0, 0, 1, 0,
    -1, 0, 0, 0,
    0, 0, 0, 1
  );
  const dashboardRotation = new BABYLON.Matrix();
  BABYLON.Matrix.FromQuaternionToRef(q, dashboardRotation);
  const sceneRotation = dashboardToSceneBasis.multiply(dashboardRotation).multiply(dashboardToSceneBasis.transpose());
  const sceneQuaternion = new BABYLON.Quaternion();
  BABYLON.Quaternion.FromRotationMatrixToRef(sceneRotation, sceneQuaternion);
  return sceneQuaternion;
}

function renderCameraPanels(cameras) {
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
    const statusText = camera.stale ? "stale" : camera.visible ? "live" : "waiting";
    if (status.textContent !== statusText) {
      status.textContent = statusText;
    }
    const frame = panel.querySelector("[data-camera-frame]");
    if (frame) {
      const frameText = `frame ${camera.frame_id || 0}`;
      if (frame.textContent !== frameText) {
        frame.textContent = frameText;
      }
      frame.title = camera.stamp_ns ? `stamp ${camera.stamp_ns}` : "stamp unavailable";
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
        <span>${escapeHtml(camera.name)}</span>
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
        <span data-camera-frame>frame --</span>
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
    streamUrl: "",
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
  const aspectKey = `${rotation}:${camera.width || 0}:${camera.height || 0}`;
  if (body.dataset.aspectKey === aspectKey) {
    return;
  }
  body.dataset.aspectKey = aspectKey;
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
  if (panel.dataset.layoutIndex === String(index)) {
    return;
  }
  panel.dataset.layoutIndex = String(index);
  panel.style.gridColumn = `${index + 1} / span 1`;
  panel.style.gridRow = "1 / span 1";
}

function updateCameraStream(panel, camera) {
  const img = panel.querySelector(".camera-frame");
  const pollState = cameraPollState.get(camera.name) || { frameUrl: "", streamUrl: "", version: -1 };
  const version = Number(camera.version || 0);
  const streamUrl = camera.stream_url || camera.frame_url;
  if (pollState.streamUrl === streamUrl && img.getAttribute("src")) {
    return;
  }
  pollState.frameUrl = camera.frame_url;
  pollState.streamUrl = streamUrl;
  pollState.version = version;
  cameraPollState.set(camera.name, pollState);
  const separator = streamUrl.includes("?") ? "&" : "?";
  img.src = `${streamUrl}${separator}ts=${Date.now()}`;
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
  const backendFps = Number(pollState.backendFps || 0);
  label.textContent = backendFps > 0 ? `${backendFps.toFixed(1)} fps` : "-- fps";
  label.title = backendFps > 0 ? `stream rx ${backendFps.toFixed(1)} fps` : "stream rx -- fps";
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
  node.metadata = { assetKey: buildAssetKey(pose), articulation: null, poseRole: pose.role };

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
  if (pose.role === "left_hand" || pose.role === "right_hand" || modelPath === UMI_ARTICULATED_MODEL_PATH) {
    await attachUmiSplitModel(pose, node);
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
    const instantiated = container.instantiateModelsToScene(() => `${pose.name}-instance`);
    const rootNode = new BABYLON.TransformNode(`${pose.name}-visual`, scene);
    rootNode.parent = node;
    const scaleMultiplier = (pose.role === "head" || pose.role === "left_hand" || pose.role === "right_hand") ? 0.2 : 1.0;
    const scaledSize = pose.avatar_scale * scaleMultiplier;
    rootNode.scaling = new BABYLON.Vector3(scaledSize, scaledSize, scaledSize);
    const rotationDeg = Array.isArray(pose.avatar_rotation_deg_xyz) ? pose.avatar_rotation_deg_xyz : [0, 0, 0];
    rootNode.rotationQuaternion = BABYLON.Quaternion.FromEulerAngles(
      BABYLON.Angle.FromDegrees(Number(rotationDeg[0] || 0)).radians(),
      BABYLON.Angle.FromDegrees(Number(rotationDeg[1] || 0)).radians(),
      BABYLON.Angle.FromDegrees(Number(rotationDeg[2] || 0)).radians()
    );
    instantiated.rootNodes.forEach((child) => {
      child.parent = rootNode;
    });
    node.metadata.articulation = extractArticulationHandles(instantiated.rootNodes, rootNode);
    collectInstantiatedMeshes(instantiated.rootNodes).forEach((mesh) => {
      mesh.material = createReadableModelMaterial(pose, mesh.material);
      mesh.visibility = 1.0;
      mesh.isPickable = false;
    });
    if (modelStatus) {
      const leftCount = node.metadata.articulation?.leftFingerNodes?.length || 0;
      const rightCount = node.metadata.articulation?.rightFingerNodes?.length || 0;
      const debugNames = (node.metadata.articulation?.debugFingerNames || []).slice(0, 6).join(",");
      modelStatus.textContent = `Models: loaded ${modelPath} [articulation L${leftCount}/R${rightCount}] [${debugNames}]`;
    }
  } catch (error) {
    warnOnce(`load:${modelPath}`, `Failed to load model ${modelPath}: ${String(error)}. Using primitive fallback.`);
    attachPrimitive(pose, node, "Model load failed, using primitive fallback");
  }
}

async function attachUmiSplitModel(pose, node) {
  const rootNode = new BABYLON.TransformNode(`${pose.name}-visual`, scene);
  rootNode.parent = node;
  const scaleMultiplier = (pose.role === "head" || pose.role === "left_hand" || pose.role === "right_hand") ? 0.2 : 1.0;
  const scaledSize = pose.avatar_scale * scaleMultiplier;
  rootNode.scaling = new BABYLON.Vector3(scaledSize, scaledSize, scaledSize);
  const rotationDeg = Array.isArray(pose.avatar_rotation_deg_xyz) ? pose.avatar_rotation_deg_xyz : [0, 0, 0];
  rootNode.rotationQuaternion = BABYLON.Quaternion.FromEulerAngles(
    BABYLON.Angle.FromDegrees(Number(rotationDeg[0] || 0)).radians(),
    BABYLON.Angle.FromDegrees(Number(rotationDeg[1] || 0)).radians(),
    BABYLON.Angle.FromDegrees(Number(rotationDeg[2] || 0)).radians()
  );

  const staticContainer = await loadModelContainer(assetUrlForPath(UMI_SPLIT_ASSETS.static), ".glb");
  const leftContainer = await loadModelContainer(assetUrlForPath(UMI_SPLIT_ASSETS.leftFinger), ".glb");
  const rightContainer = await loadModelContainer(assetUrlForPath(UMI_SPLIT_ASSETS.rightFinger), ".glb");

  const leftMotionNode = new BABYLON.TransformNode(`${pose.name}-left-finger-motion`, scene);
  leftMotionNode.parent = rootNode;
  const rightMotionNode = new BABYLON.TransformNode(`${pose.name}-right-finger-motion`, scene);
  rightMotionNode.parent = rootNode;

  const staticInstance = staticContainer.instantiateModelsToScene(() => `${pose.name}-umi-static`);
  const leftInstance = leftContainer.instantiateModelsToScene(() => `${pose.name}-umi-left`);
  const rightInstance = rightContainer.instantiateModelsToScene(() => `${pose.name}-umi-right`);

  staticInstance.rootNodes.forEach((child) => {
    child.parent = rootNode;
  });
  leftInstance.rootNodes.forEach((child) => {
    child.parent = leftMotionNode;
  });
  rightInstance.rootNodes.forEach((child) => {
    child.parent = rightMotionNode;
  });

  const allMeshes = [
    ...collectInstantiatedMeshes(staticInstance.rootNodes),
    ...collectInstantiatedMeshes(leftInstance.rootNodes),
    ...collectInstantiatedMeshes(rightInstance.rootNodes)
  ];
  allMeshes.forEach((mesh) => {
    mesh.material = createReadableModelMaterial(pose, mesh.material);
    mesh.visibility = 1.0;
    mesh.isPickable = false;
  });

  node.metadata.articulation = {
    leftFingerNodes: [leftMotionNode],
    rightFingerNodes: [rightMotionNode],
    leftBasePositions: [leftMotionNode.position.clone()],
    rightBasePositions: [rightMotionNode.position.clone()],
    debugFingerNames: ["split:left_motion", "split:right_motion"]
  };
  if (modelStatus) {
    modelStatus.textContent = `Models: split UMI loaded for ${pose.name}`;
  }
}

function assetUrlForPath(path) {
  return `/asset?path=${encodeURIComponent(path)}`;
}

async function loadModelContainer(url, pluginExtension) {
  if (!modelPromises.has(url)) {
    modelPromises.set(
      url,
      BABYLON.SceneLoader.LoadAssetContainerAsync("", url, scene, null, pluginExtension)
    );
  }
  return modelPromises.get(url);
}

function applyPoseArticulation(pose, node) {
  const articulation = node?.metadata?.articulation;
  if (!articulation) {
    return;
  }
  const openRatio = resolveGripperOpenRatio(pose);
  applyFingerMotion(articulation.leftFingerNodes || [], articulation.leftBasePositions || [], openRatio, +1);
  applyFingerMotion(articulation.rightFingerNodes || [], articulation.rightBasePositions || [], openRatio, -1);
}

function resolveGripperOpenRatio(pose) {
  if (manualGripperOpenRatio !== null) {
    return manualGripperOpenRatio;
  }
  const candidate = Number(pose.gripper_open_ratio);
  if (!Number.isFinite(candidate)) {
    return 0.0;
  }
  return BABYLON.Scalar.Clamp(candidate, 0.0, 1.0);
}

function initializeGripperKeyboardControls() {
  if (canvas) {
    canvas.tabIndex = 0;
    canvas.style.outline = "none";
    window.setTimeout(() => canvas.focus(), 50);
    canvas.addEventListener("pointerdown", () => canvas.focus());
  }

  const handleKeyDown = (event) => {
    if (event.repeat) {
      return;
    }
    const key = String(event.key || "").toLowerCase();
    if (key === "n") {
      manualGripperOpenRatio = 0.0;
      refreshAllArticulations();
      updateGripperDebugStatus("close");
    } else if (key === "m") {
      manualGripperOpenRatio = 1.0;
      refreshAllArticulations();
      updateGripperDebugStatus("open");
    } else if (key === "b") {
      manualGripperOpenRatio = null;
      if (modelStatus) {
        modelStatus.textContent = "Models: gripper override cleared [B]";
      }
      refreshAllArticulations();
    } else {
      return;
    }
    event.preventDefault();
  };

  window.addEventListener("keydown", handleKeyDown, true);
  document.addEventListener("keydown", handleKeyDown, true);
}

function refreshAllArticulations() {
  for (const node of poseNodes.values()) {
    const role = node?.metadata?.poseRole;
    if (role !== "left_hand" && role !== "right_hand") {
      continue;
    }
    applyPoseArticulation({ role, gripper_open_ratio: 0.0 }, node);
  }
}

function applyFingerMotion(nodes, basePositions, openRatio, direction) {
  nodes.forEach((fingerNode, index) => {
    const base = basePositions[index];
    if (!fingerNode || !base) {
      return;
    }
    fingerNode.position.copyFrom(base);
    fingerNode.position.x += GRIPPER_SLIDER_TRAVEL_METERS * openRatio * direction;
    fingerNode.computeWorldMatrix(true);
  });
}

function updateGripperDebugStatus(modeLabel) {
  if (!modelStatus) {
    return;
  }
  const segments = [];
  for (const [poseName, node] of poseNodes.entries()) {
    const role = node?.metadata?.poseRole;
    if (role !== "left_hand" && role !== "right_hand") {
      continue;
    }
    const articulation = node?.metadata?.articulation;
    const leftNode = articulation?.leftFingerNodes?.[0];
    const rightNode = articulation?.rightFingerNodes?.[0];
    const leftX = leftNode ? Number(leftNode.position.x).toFixed(3) : "-";
    const rightX = rightNode ? Number(rightNode.position.x).toFixed(3) : "-";
    const leftCount = articulation?.leftFingerNodes?.length || 0;
    const rightCount = articulation?.rightFingerNodes?.length || 0;
    segments.push(`${poseName} L${leftCount}:${leftX} R${rightCount}:${rightX}`);
  }
  modelStatus.textContent = `Models: gripper ${modeLabel} [${segments.join(" | ")}]`;
}

function extractArticulationHandles(instantiatedRootNodes, rootNode) {
  const nodes = [
    rootNode,
    ...(instantiatedRootNodes || []),
    ...((instantiatedRootNodes || []).flatMap((node) => node.getDescendants(false))),
    ...rootNode.getDescendants(false)
  ];
  const leftFingerNodes = [];
  const rightFingerNodes = [];
  const debugFingerNames = [];
  nodes.forEach((child) => {
    const rawName = String(child?.name || child?.id || "");
    const childName = rawName.toLowerCase();
    if (childName.includes("finger")) {
      debugFingerNames.push(rawName);
    }
    if (
      childName.includes("left_finger_slider_left_finger") ||
      (childName.includes("left_finger") && !childName.includes("holder"))
    ) {
      leftFingerNodes.push(child);
    }
    if (
      childName.includes("right_finger_slider_right_finger") ||
      (childName.includes("right_finger") && !childName.includes("holder"))
    ) {
      rightFingerNodes.push(child);
    }
  });
  if (!leftFingerNodes.length && !rightFingerNodes.length) {
    return { leftFingerNodes: [], rightFingerNodes: [], leftBasePositions: [], rightBasePositions: [], debugFingerNames };
  }
  return {
    leftFingerNodes,
    rightFingerNodes,
    leftBasePositions: leftFingerNodes.map((node) => node.position.clone()),
    rightBasePositions: rightFingerNodes.map((node) => node.position.clone()),
    debugFingerNames
  };
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
  return `${pose.avatar_model || "primitive"}:${pose.avatar_scale || 1}`;
}

function warnOnce(key, message) {
  if (modelWarnings.has(key)) {
    return;
  }
  modelWarnings.add(key);
  console.warn(message);
}

function bindTrailToggles() {
  if (!legend || legend.dataset.trailToggleBound === "true") {
    return;
  }
  legend.dataset.trailToggleBound = "true";
  legend.addEventListener("change", (event) => {
    const target = event.target;
    if (!target || target.tagName !== "INPUT" || !target.matches("input[data-trail-key]")) {
      return;
    }
    const role = target.getAttribute("data-role");
    const trailKey = target.getAttribute("data-trail-key");
    setTrailEnabled(trailKey, target.checked, role);
  });
}

function updateTrails() {
  for (const trail of trailStates.values()) {
    if (!trail.enabled) {
      disposeTrailMesh(trail);
    }
  }
}

function ensureTrailState(key, role = key) {
  if (trailStates.has(key)) {
    return trailStates.get(key);
  }
  const state = {
    key,
    role,
    enabled: DEFAULT_TRAIL_ENABLED[role] !== false,
    points: [],
    mesh: null,
    segmentMeshes: [],
    nextSegmentId: 0
  };
  trailStates.set(key, state);
  return state;
}

function setTrailEnabled(key, enabled, role = key) {
  const trail = ensureTrailState(key, role);
  trail.enabled = Boolean(enabled);
  currentLegendSignature = "";
  renderPoseLegend(latestLegendPoses);
  if (!trail.enabled) {
    disposeTrailMesh(trail);
    return;
  }
  refreshAllTrails();
}

function isTrailEnabled(key, role = key) {
  return ensureTrailState(key, role).enabled;
}

function clearTrail(trail) {
  trail.points = [];
  disposeTrailMesh(trail);
}

function disposeTrailMesh(trail) {
  if (trail.mesh) {
    trail.mesh.dispose(false, false);
    trail.mesh = null;
  }
  if (trail.segmentMeshes) {
    trail.segmentMeshes.forEach(disposeTrailSegment);
    trail.segmentMeshes = [];
  }
  trail.nextSegmentId = 0;
}

function disposeTrailSegment(mesh) {
  if (!mesh) {
    return;
  }
  mesh.material = null;
  mesh.dispose(false, false);
}

function loadTrailWidthMultiplier() {
  const stored = Number(window.localStorage.getItem(TRAIL_WIDTH_STORAGE_KEY) || "1");
  if (Number.isFinite(stored) && stored >= 0.1 && stored <= 1.5) {
    return stored;
  }
  return 1.0;
}

function initializeTrailWidthControls() {
  if (!trailWidthSlider || !trailWidthValue) {
    return;
  }
  trailWidthSlider.value = String(trailWidthMultiplier);
  updateTrailWidthLabel();
  trailWidthSlider.addEventListener("input", (event) => {
    const next = Number(event.currentTarget.value || 1);
    trailWidthMultiplier = Number.isFinite(next) ? next : 1.0;
    window.localStorage.setItem(TRAIL_WIDTH_STORAGE_KEY, String(trailWidthMultiplier));
    updateTrailWidthLabel();
    refreshAllTrails();
  });
}

function initializeTrailRetentionControls() {
  if (!trailKeepAllToggle && !trailClearButton) {
    return;
  }
  refreshTrailSettings();
  trailKeepAllToggle?.addEventListener("change", updateTrailRetention);
  trailClearButton?.addEventListener("click", clearStoredTrails);
}

async function refreshTrailSettings() {
  try {
    renderTrailSettings(await fetchJson("/api/trails/settings"));
  } catch (error) {
    setTrailStatus(`Trail setting unavailable: ${error.message}`);
  }
}

async function updateTrailRetention() {
  if (!trailKeepAllToggle) {
    return;
  }
  setTrailControlsBusy(true);
  try {
    const settings = await fetchJson("/api/trails/settings", {
      method: "POST",
      body: JSON.stringify({ keep_all_points: trailKeepAllToggle.checked })
    });
    renderTrailSettings(settings);
  } catch (error) {
    trailKeepAllToggle.checked = !trailKeepAllToggle.checked;
    setTrailStatus(`Trail setting failed: ${error.message}`);
  } finally {
    setTrailControlsBusy(false);
  }
}

async function clearStoredTrails() {
  setTrailControlsBusy(true);
  try {
    const settings = await fetchJson("/api/trails/clear", { method: "POST", body: "{}" });
    ignoreTrailUpdatesUntilMs = Date.now() + 400;
    clearAllTrailMeshes();
    renderTrailSettings(settings);
    setTrailStatus("Trail cleared; collecting new points");
  } catch (error) {
    setTrailStatus(`Clear trail failed: ${error.message}`);
  } finally {
    setTrailControlsBusy(false);
  }
}

function renderTrailSettings(settings) {
  const keepAll = Boolean(settings.keep_all_points);
  keepAllTrailPoints = keepAll;
  trailMaxPoints = Number(settings.max_points || 300);
  if (trailKeepAllToggle) {
    trailKeepAllToggle.checked = keepAll;
  }
  setTrailStatus(keepAll ? "Keeping all points" : `Keeping latest ${settings.max_points || 300} points`);
}

function setTrailStatus(message) {
  if (trailStatus) {
    trailStatus.textContent = message;
  }
}

function setTrailControlsBusy(isBusy) {
  [trailKeepAllToggle, trailClearButton].forEach((control) => {
    if (control) {
      control.disabled = isBusy;
      control.classList.toggle("is-busy", isBusy);
    }
  });
}

function updateTrailWidthLabel() {
  if (trailWidthValue) {
    trailWidthValue.textContent = `${trailWidthMultiplier.toFixed(1)}x`;
  }
}

function clearAllTrailMeshes() {
  for (const trail of trailStates.values()) {
    clearTrail(trail);
  }
}

function applyTraceGeneration(generation) {
  if (generation === undefined || generation === null) {
    return;
  }
  if (currentTraceGeneration === null) {
    currentTraceGeneration = generation;
    return;
  }
  if (generation !== currentTraceGeneration) {
    currentTraceGeneration = generation;
    clearAllTrailMeshes();
  }
}

function applyPoseSource(source) {
  if (!source) {
    return;
  }
  if (currentPoseSource === null) {
    currentPoseSource = source;
    return;
  }
  if (source !== currentPoseSource) {
    currentPoseSource = source;
    currentTraceGeneration = null;
    clearAllTrailMeshes();
  }
}

function refreshAllTrails() {
  for (const trail of trailStates.values()) {
    disposeTrailMesh(trail);
    if (trail.enabled && trail.points.length >= 2) {
      rebuildTrailSegments(trail);
    }
  }
}

function updateTrailFromPose(pose) {
  if (Date.now() < ignoreTrailUpdatesUntilMs) {
    return;
  }
  const trail = ensureTrailState(trailKeyForPose(pose), pose.role);
  if (!pose.visible) {
    return;
  }
  const point = mapDashboardPositionToScene(pose.position || [0, 0, 0]);
  const changed = appendTrailPoint(trail, point);
  trimTrailPoints(trail);
  if (trail.points.length < 2) {
    return;
  }
  if (!trail.enabled) {
    disposeTrailMesh(trail);
    return;
  }
  if (changed) {
    appendTrailSegment(trail);
  }
}

function appendTrailPoint(trail, point) {
  if (!trail.points.length) {
    trail.points.push(point);
    return false;
  }
  const lastPoint = trail.points[trail.points.length - 1];
  if (BABYLON.Vector3.Distance(lastPoint, point) <= 0.002) {
    return false;
  }
  trail.points.push(point);
  return true;
}

function trimTrailPoints(trail) {
  if (keepAllTrailPoints) {
    return;
  }
  const maxPoints = Number.isFinite(trailMaxPoints) ? trailMaxPoints : 300;
  if (trail.points.length > maxPoints) {
    const removeCount = trail.points.length - maxPoints;
    trail.points.splice(0, removeCount);
    const removedSegments = (trail.segmentMeshes || []).splice(0, removeCount);
    removedSegments.forEach(disposeTrailSegment);
  }
}

function rebuildTrailSegments(trail) {
  for (let index = 1; index < trail.points.length; index += 1) {
    createTrailSegment(trail, trail.points[index - 1], trail.points[index]);
  }
}

function appendTrailSegment(trail) {
  if (!trail.enabled || trail.points.length < 2) {
    return;
  }
  const lastIndex = trail.points.length - 1;
  createTrailSegment(trail, trail.points[lastIndex - 1], trail.points[lastIndex]);
}

function createTrailSegment(trail, startPoint, endPoint) {
  if (!scene) {
    return;
  }
  if (BABYLON.Vector3.Distance(startPoint, endPoint) <= 0.002) {
    return;
  }
  const roleColor = BABYLON.Color3.FromHexString((ROLE_STYLE[trail.role] || ROLE_STYLE.head).color);
  const radius = (TRAIL_RADIUS_BY_ROLE[trail.role] || 0.016) * trailWidthMultiplier;
  const meshKey = String(trail.key || trail.role).replace(/[^a-zA-Z0-9_-]/g, "-");
  const noCap = BABYLON.Mesh?.NO_CAP ?? 0;
  const segment = BABYLON.MeshBuilder.CreateTube(
    `trail-${meshKey}-segment-${trail.nextSegmentId}`,
    { path: [startPoint.clone(), endPoint.clone()], radius, tessellation: 8, cap: noCap, updatable: false },
    scene
  );
  trail.nextSegmentId += 1;
  segment.isPickable = false;
  segment.alwaysSelectAsActiveMesh = true;
  segment.renderingGroupId = 1;
  if (!trail.material) {
    const material = new BABYLON.StandardMaterial(`trail-mat-${meshKey}`, scene);
    material.disableLighting = true;
    material.emissiveColor = roleColor;
    material.diffuseColor = roleColor;
    material.specularColor = BABYLON.Color3.Black();
    material.ambientColor = roleColor;
    material.alpha = 0.96;
    trail.material = material;
  }
  trail.material.emissiveColor = roleColor;
  trail.material.diffuseColor = roleColor;
  segment.material = trail.material;
  trail.segmentMeshes.push(segment);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function initializePostProcessingPanel() {
  if (!refreshBagsButton && !imageSourceSelect && !actionBagSelects.length && !startRecordingButton) {
    return;
  }
  refreshBagsButton?.addEventListener("click", refreshRosbags);
  imageSourceSelect?.addEventListener("change", () => {
    selectedBagName = imageSourceSelect.value;
    syncBagSelects(selectedBagName);
    renderBagList(knownBags);
    if (!selectedBagName) {
      stopImagePlayback({ clearSelection: true });
    } else {
      renderImagePlaybackStatus({ playing: false });
    }
  });
  imageLiveButton?.addEventListener("click", () => stopImagePlayback({ clearSelection: true }));
  imagePlayButton?.addEventListener("click", playSelectedImageBag);
  imageStopButton?.addEventListener("click", () => stopImagePlayback());
  actionBagSelects.forEach((select) => {
    select.addEventListener("change", () => {
      selectedBagName = select.value;
      syncBagSelects(selectedBagName);
      renderBagList(knownBags);
      setPostprocessOutput(selectedBagName ? `Selected ${selectedBagName}` : "No rosbag selected.");
    });
  });
  startRecordingButton?.addEventListener("click", startRecording);
  stopRecordingButton?.addEventListener("click", stopRecording);
  refreshRecordTopicsButton?.addEventListener("click", refreshRecordTopics);
  alignmentStartButton?.addEventListener("click", startAlignment);
  alignmentStopButton?.addEventListener("click", stopAlignment);
  for (const button of document.querySelectorAll("[data-post-action]")) {
    button.addEventListener("click", () => runPostprocessAction(button.dataset.postAction));
  }
  refreshRosbags();
  refreshRecordingStatus({ refreshTopics: currentView === "recording" });
  refreshImagePlaybackStatus();
  window.setInterval(() => refreshRecordingStatus({ refreshTopics: false }), 1500);
  window.setInterval(refreshImagePlaybackStatus, 1500);
  window.setInterval(refreshAlignmentStatus, 1500);
}

function initializeMonitorDashboardLauncher() {
  if (!monitorDashboardButtons.length && !monitorDashboardStatuses.length) {
    return;
  }
  monitorDashboardButtons.forEach((button) => {
    button.addEventListener("click", toggleMonitorDashboard);
  });
  refreshMonitorDashboardStatus();
  window.setInterval(refreshMonitorDashboardStatus, 1500);
}

async function refreshMonitorDashboardStatus() {
  if (!monitorDashboardStatuses.length) {
    return;
  }
  try {
    const status = await fetchJson("/api/monitor-dashboard/status");
    renderMonitorDashboardStatus(status);
  } catch (error) {
    monitorDashboardRunning = false;
    updateAlignmentStartAvailability();
    setMonitorDashboardStatus(`本地监控窗口状态不可用：${error.message}`);
  }
}

async function toggleMonitorDashboard() {
  if (!monitorDashboardButtons.length) {
    return;
  }
  setMonitorDashboardBusy(true);
  try {
    const current = await fetchJson("/api/monitor-dashboard/status");
    const endpoint = current.running ? "/api/monitor-dashboard/stop" : "/api/monitor-dashboard/start";
    const status = await fetchJson(endpoint, { method: "POST", body: "{}" });
    renderMonitorDashboardStatus(status);
  } catch (error) {
    setMonitorDashboardStatus(`操作失败：${error.message}`);
  } finally {
    setMonitorDashboardBusy(false);
  }
}

function renderMonitorDashboardStatus(status) {
  if (!monitorDashboardStatuses.length) {
    return;
  }
  if (status.running) {
    monitorDashboardRunning = true;
    const pid = Number(status.pid || 0);
    setMonitorDashboardStatus(pid > 0
      ? `本地监控窗口已启动 (PID ${pid})`
      : "本地监控窗口已启动");
    setMonitorDashboardButtonText("关闭本地 Monitor Dashboard");
    updateAlignmentStartAvailability();
    return;
  }
  monitorDashboardRunning = false;
  setMonitorDashboardButtonText("打开本地 Monitor Dashboard");
  setMonitorDashboardStatus("本地监控窗口未启动");
  updateAlignmentStartAvailability();
}

function setMonitorDashboardBusy(isBusy) {
  monitorDashboardButtons.forEach((button) => {
    button.disabled = isBusy;
    button.classList.toggle("is-busy", isBusy);
  });
}

function setMonitorDashboardStatus(message) {
  monitorDashboardStatuses.forEach((status) => {
    status.textContent = message;
  });
}

function setMonitorDashboardButtonText(message) {
  monitorDashboardButtons.forEach((button) => {
    button.textContent = message;
  });
}

async function refreshRosbags() {
  if (!imageSourceSelect && !actionBagSelects.length && !bagList) {
    return;
  }
  try {
    const payload = await fetchJson("/api/bags");
    const previous = selectedBagName || imageSourceSelect?.value || "";
    const bags = payload.bags || [];
    knownBags = bags;
    if (bagRoot) {
      bagRoot.textContent = `Root: ${payload.rosbag_root || "unknown"}`;
    }
    if (imageSourceSelect) {
      imageSourceSelect.innerHTML = "";
      imageSourceSelect.append(new Option("Live stream", ""));
    }
    if (!bags.length) {
      populateActionBagSelects([]);
      selectedBagName = "";
      renderBagList([]);
      if (bagSummary) {
        bagSummary.textContent = "0 bags found";
      }
      return;
    }
    for (const bag of bags) {
      const label = `${bag.name} (${formatBytes(bag.size_bytes)})`;
      imageSourceSelect?.append(new Option(label, bag.name));
    }
    populateActionBagSelects(bags);
    selectedBagName = bags.some((bag) => bag.name === previous) ? previous : "";
    syncBagSelects(selectedBagName);
    renderBagList(bags);
    if (bagSummary) {
      bagSummary.textContent = `${bags.length} bag${bags.length === 1 ? "" : "s"} found`;
    }
  } catch (error) {
    setPostprocessOutput(`Bag refresh failed: ${error.message}`);
  }
}

async function playSelectedImageBag() {
  if (!selectedBagName) {
    setImagePlaybackStatus("Select a rosbag to play.");
    return;
  }
  setImagePlaybackBusy(true);
  try {
    const status = await fetchJson("/api/playback/start", {
      method: "POST",
      body: JSON.stringify({ bag_id: selectedBagName })
    });
    renderImagePlaybackStatus(status);
    await pollCameraMetadata();
  } catch (error) {
    setImagePlaybackStatus(`Playback failed: ${error.message}`);
  } finally {
    setImagePlaybackBusy(false);
  }
}

async function stopImagePlayback({ clearSelection = false } = {}) {
  setImagePlaybackBusy(true);
  try {
    const status = await fetchJson("/api/playback/stop", { method: "POST", body: "{}" });
    if (clearSelection && imageSourceSelect) {
      imageSourceSelect.value = "";
    }
    if (clearSelection) {
      selectedBagName = "";
    }
    renderImagePlaybackStatus(status);
    await pollCameraMetadata();
  } catch (error) {
    setImagePlaybackStatus(`Stop playback failed: ${error.message}`);
  } finally {
    setImagePlaybackBusy(false);
  }
}

async function refreshImagePlaybackStatus() {
  if (!imagePlaybackStatus) {
    return;
  }
  try {
    renderImagePlaybackStatus(await fetchJson("/api/playback/status"));
  } catch (error) {
    setImagePlaybackStatus(`Playback status unavailable: ${error.message}`);
  }
}

function renderImagePlaybackStatus(status) {
  if (status.playing) {
    const source = status.play_path || status.bag || "selected bag";
    setImagePlaybackStatus(`Playing ${source}`);
    if (imagePlayButton) {
      imagePlayButton.disabled = true;
    }
    if (imageStopButton) {
      imageStopButton.disabled = false;
    }
    return;
  }
  setImagePlaybackStatus("Live stream");
  if (imagePlayButton) {
    imagePlayButton.disabled = !selectedBagName;
  }
  if (imageStopButton) {
    imageStopButton.disabled = true;
  }
}

function setImagePlaybackStatus(message) {
  if (imagePlaybackStatus) {
    imagePlaybackStatus.textContent = message;
  }
}

function setImagePlaybackBusy(isBusy) {
  [imageLiveButton, imagePlayButton, imageStopButton].forEach((button) => {
    if (button) {
      button.classList.toggle("is-busy", isBusy);
    }
  });
}

function renderBagList(bags) {
  if (!bagList) {
    return;
  }
  if (!bags.length) {
    bagList.innerHTML = `<div class="empty-state">No rosbags found.</div>`;
    return;
  }
  bagList.innerHTML = bags.map((bag) => {
    const topics = (bag.topics || []).slice(0, 4).map((topic) => escapeHtml(topic.short_name || topic.name)).join(", ");
    return `
      <button class="bag-row ${bag.name === selectedBagName ? "selected" : ""}" type="button" data-bag-name="${escapeHtml(bag.name)}">
        <div>
          <strong>${escapeHtml(bag.name)}</strong>
          <span>${escapeHtml(bag.path)}</span>
        </div>
        <div>${formatBytes(bag.size_bytes)}</div>
        <div>${formatDuration(bag.duration_sec)}</div>
        <div>${topics || "topics pending"}</div>
        <div class="result-chips">${renderResultChips(bag.result_statuses || {})}</div>
      </button>`;
  }).join("");
  bagList.querySelectorAll("[data-bag-name]").forEach((row) => {
    row.addEventListener("click", () => {
      selectedBagName = row.dataset.bagName;
      syncBagSelects(selectedBagName);
      renderBagList(knownBags);
      setPostprocessOutput(`Selected ${selectedBagName}`);
    });
  });
}

async function refreshRecordingStatus(options = {}) {
  if (!recordingStatus) {
    return;
  }
  if (currentView !== "recording" && !options.force) {
    return;
  }
  try {
    const status = await fetchJson("/api/recording/status");
    renderRecordingStatus(status);
    if (options.refreshTopics) {
      const catalog = await fetchJson("/api/recording/topics");
      renderTopicCatalog(catalog);
      setRecordTopicStatus(catalog);
    } else {
      renderTopicCatalog(status.topic_catalog);
      setRecordTopicStatus(status.topic_catalog);
    }
  } catch (error) {
    recordingStatus.textContent = `Recording status unavailable: ${error.message}`;
  }
}

async function refreshRecordTopics() {
  if (!recordTopicGroups) {
    return;
  }
  setRecordTopicRefreshBusy(true);
  try {
    const catalog = await fetchJson("/api/recording/topics");
    renderTopicCatalog(catalog, { resetSelection: true });
    setRecordTopicStatus(catalog, "Topic list refreshed");
  } catch (error) {
    if (recordTopicStatus) {
      recordTopicStatus.textContent = `Topic refresh failed: ${error.message}`;
    }
  } finally {
    setRecordTopicRefreshBusy(false);
  }
}

function setRecordTopicRefreshBusy(isBusy) {
  if (refreshRecordTopicsButton) {
    refreshRecordTopicsButton.disabled = isBusy;
    refreshRecordTopicsButton.classList.toggle("is-busy", isBusy);
  }
}

function setRecordTopicStatus(catalog, prefix = "Topic list ready") {
  if (!recordTopicStatus || !catalog) {
    return;
  }
  const count = Array.isArray(catalog.topics) ? catalog.topics.length : 0;
  recordTopicStatus.textContent = `${prefix}: ${count} topic${count === 1 ? "" : "s"}`;
}

async function startRecording() {
  setRecordingBusy(true);
  try {
    if (recordTopicGroups) {
      await refreshRecordingStatus({ refreshTopics: true, force: true });
    }
    const topics = recordTopicGroups
      ? Array.from(recordTopicGroups.querySelectorAll("[data-record-topic]:checked")).map((checkbox) => checkbox.value)
      : Array.from(selectedRecordTopics);
    if (recordTopicGroups && topics.length === 0) {
      throw new Error("Select at least one topic to record.");
    }
    const body = recordTopicGroups ? JSON.stringify({ topics }) : "{}";
    const status = await fetchJson("/api/recording/start", { method: "POST", body });
    renderRecordingStatus(status);
    setPostprocessOutput(`Recording started: ${status.output_path || "pending output path"}`);
  } catch (error) {
    setPostprocessOutput(`Start recording failed: ${error.message}`);
  } finally {
    setRecordingBusy(false);
  }
}

async function stopRecording() {
  setRecordingBusy(true);
  try {
    const status = await fetchJson("/api/recording/stop", { method: "POST", body: "{}" });
    renderRecordingStatus(status);
    await refreshRosbags();
    setPostprocessOutput("Recording stopped.");
  } catch (error) {
    setPostprocessOutput(`Stop recording failed: ${error.message}`);
  } finally {
    setRecordingBusy(false);
  }
}

async function refreshAlignmentStatus() {
  if (!alignmentLogOutput || currentView !== "alignment") {
    return;
  }
  try {
    renderAlignmentStatus(await fetchJson("/api/alignment/status"));
  } catch (error) {
    alignmentLogOutput.textContent = `Alignment status unavailable: ${error.message}`;
  }
}

async function startAlignment() {
  if (!monitorDashboardRunning) {
    if (alignmentLogOutput) {
      alignmentLogOutput.textContent = "请先打开本地 Monitor Dashboard，再开始校准。";
    }
    updateAlignmentStartAvailability();
    return;
  }
  setAlignmentBusy(true);
  try {
    renderAlignmentStatus(await fetchJson("/api/alignment/start", { method: "POST", body: "{}" }));
  } catch (error) {
    if (alignmentLogOutput) {
      alignmentLogOutput.textContent = `Start alignment failed: ${error.message}`;
    }
  } finally {
    setAlignmentBusy(false);
  }
}

async function stopAlignment() {
  setAlignmentBusy(true);
  try {
    renderAlignmentStatus(await fetchJson("/api/alignment/stop", { method: "POST", body: "{}" }));
  } catch (error) {
    if (alignmentLogOutput) {
      alignmentLogOutput.textContent = `Stop alignment failed: ${error.message}`;
    }
  } finally {
    setAlignmentBusy(false);
  }
}

function renderAlignmentStatus(status) {
  if (!alignmentLogOutput) {
    return;
  }
  latestAlignmentStatus = status;
  const logs = Array.isArray(status.logs) ? status.logs : [];
  const header = [
    `Status: ${status.status || "unknown"}`,
    `Reference: ${status.reference_camera || "-"}`,
    `Result: ${status.result_txt || "-"}`
  ];
  alignmentLogOutput.textContent = `${header.join("\n")}\n\n${logs.length ? logs.join("\n") : "No alignment logs yet."}`;
  updateAlignmentStartAvailability();
  if (alignmentStopButton) {
    alignmentStopButton.disabled = !status.active;
  }
}

function updateAlignmentStartAvailability() {
  const active = Boolean(latestAlignmentStatus?.active);
  if (alignmentStartButton) {
    alignmentStartButton.disabled = active || !monitorDashboardRunning;
    alignmentStartButton.title = monitorDashboardRunning
      ? ""
      : "请先打开本地 Monitor Dashboard";
  }
  if (alignmentStartHint) {
    alignmentStartHint.textContent = monitorDashboardRunning
      ? "本地 Monitor Dashboard 已启动，可以开始校准。"
      : "请先打开本地 Monitor Dashboard，再开始校准。";
  }
}

function setAlignmentBusy(isBusy) {
  [alignmentStartButton, alignmentStopButton].forEach((button) => {
    if (button) {
      button.classList.toggle("is-busy", isBusy);
    }
  });
}

async function runPostprocessAction(action) {
  const actionSelect = actionBagSelects.find((select) => select.dataset.actionBag === action);
  const bagName = actionSelect?.value || selectedBagName;
  if (!bagName) {
    setPostprocessOutput("Select a rosbag before running post processing.");
    return;
  }
  selectedBagName = bagName;
  syncBagSelects(selectedBagName);
  setPostprocessOutput(`Running ${action} on ${selectedBagName}...`);
  try {
    const result = await fetchJson(`/api/process/${encodeURIComponent(action)}`, {
      method: "POST",
      body: JSON.stringify({ bag_id: selectedBagName, options: {} })
    });
    setPostprocessOutput(JSON.stringify(result, null, 2));
    if (jobSummary) {
      jobSummary.textContent = `${result.action}: ${result.status}`;
    }
    await refreshRosbags();
  } catch (error) {
    setPostprocessOutput(`Post processing failed: ${error.message}`);
  }
}

function populateActionBagSelects(bags) {
  actionBagSelects.forEach((select) => {
    const previous = select.value;
    select.innerHTML = "";
    if (!bags.length) {
      select.append(new Option("No rosbags found", ""));
      return;
    }
    for (const bag of bags) {
      select.append(new Option(`${bag.name} (${formatBytes(bag.size_bytes)})`, bag.name));
    }
    select.value = bags.some((bag) => bag.name === previous) ? previous : bags[0].name;
  });
}

function syncBagSelects(value) {
  if (imageSourceSelect) {
    imageSourceSelect.value = value;
  }
  actionBagSelects.forEach((select) => {
    if (value) {
      select.value = value;
    }
  });
}

function renderResultChips(statuses) {
  const labels = [
    ["trajectory-scoring", "Score"],
    ["trajectory-optimization", "Optimize"],
    ["coordinate-alignment", "Align"]
  ];
  return labels
    .filter(([key]) => key !== "coordinate-alignment" || Boolean(statuses[key]?.ready))
    .map(([key, label]) => {
      const ready = Boolean(statuses[key]?.ready);
      return `<span class="result-chip ${ready ? "ready" : ""}">${ready ? label : `No ${label}`}</span>`;
    })
    .join("");
}

function renderTopicCatalog(catalog, options = {}) {
  if (!recordTopicGroups || !catalog) {
    return;
  }
  const allTopics = Array.isArray(catalog.topics) ? catalog.topics : [];
  if (!recordTopicsInitialized || options.resetSelection) {
    selectedRecordTopics = new Set(allTopics);
    knownRecordTopics = new Set(allTopics);
    recordTopicsInitialized = true;
  } else {
    for (const topic of allTopics) {
      if (!knownRecordTopics.has(topic)) {
        selectedRecordTopics.add(topic);
      }
    }
    knownRecordTopics = new Set(allTopics);
  }
  const cameraGroups = Array.isArray(catalog.cameras) ? catalog.cameras : [];
  const visibleCameraGroups = cameraGroups.filter(
    (group) => group.detected && Array.isArray(group.topics) && group.topics.length > 0
  );
  const otherTopics = Array.isArray(catalog.other) ? catalog.other : [];
  const cameraHtml = visibleCameraGroups.length
    ? visibleCameraGroups.map(renderCameraTopicGroup).join("")
    : `<div class="empty-state">No live camera topics found.</div>`;
  const otherHtml = otherTopics.length
    ? renderTopicList("Other", otherTopics)
    : `<div class="empty-state">No other topics configured.</div>`;
  recordTopicGroups.innerHTML = `${cameraHtml}${otherHtml}`;
  recordTopicGroups.querySelectorAll("[data-record-camera-toggle]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const group = checkbox.closest(".topic-group");
      if (!group) {
        return;
      }
      group.querySelectorAll("[data-record-topic]").forEach((topicCheckbox) => {
        topicCheckbox.checked = checkbox.checked;
        if (topicCheckbox.checked) {
          selectedRecordTopics.add(topicCheckbox.value);
        } else {
          selectedRecordTopics.delete(topicCheckbox.value);
        }
      });
    });
  });
  recordTopicGroups.querySelectorAll("[data-record-topic]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        selectedRecordTopics.add(checkbox.value);
      } else {
        selectedRecordTopics.delete(checkbox.value);
      }
    });
  });
}

function renderCameraTopicGroup(group) {
  if (!group.detected) {
    return `<details class="topic-group" open><summary>${escapeHtml(group.label || group.name)}</summary><div class="empty-state">Camera not detected.</div></details>`;
  }
  if (!group.topics || !group.topics.length) {
    return `<details class="topic-group" open><summary>${escapeHtml(group.label || group.name)}</summary><div class="empty-state">No configured topics for this camera.</div></details>`;
  }
  return renderTopicList(group.label || group.name, group.topics);
}

function renderTopicList(title, topics) {
  const allChecked = topics.length > 0 && topics.every((topic) => selectedRecordTopics.has(topic.name));
  return `
    <details class="topic-group" open>
      <summary>
        <label class="topic-group-toggle">
          <input type="checkbox" data-record-camera-toggle ${allChecked ? "checked" : ""}>
          <span>${escapeHtml(title)}</span>
        </label>
      </summary>
      <div class="topic-list">
        ${topics.map((topic) => `
          <label class="topic-option">
            <input type="checkbox" data-record-topic value="${escapeHtml(topic.name)}" ${selectedRecordTopics.has(topic.name) ? "checked" : ""}>
            <span>${escapeHtml(topic.label || topic.tail || topic.name)}</span>
          </label>
        `).join("")}
      </div>
    </details>`;
}

function renderRecordingStatus(status) {
  const active = Boolean(status.recording);
  recordingStatus.textContent = active
    ? `Recording to ${status.output_path || "pending path"}`
    : "Recording idle";
  if (startRecordingButton) {
    startRecordingButton.disabled = active;
  }
  if (stopRecordingButton) {
    stopRecordingButton.disabled = !active;
  }
}

function setRecordingBusy(isBusy) {
  if (startRecordingButton) {
    startRecordingButton.classList.toggle("is-busy", isBusy);
  }
  if (stopRecordingButton) {
    stopRecordingButton.classList.toggle("is-busy", isBusy);
  }
}

function setPostprocessOutput(message) {
  if (postprocessOutput) {
    postprocessOutput.textContent = message;
  }
  workflowOutputs.forEach((output) => {
    output.textContent = message;
  });
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `${response.status} ${response.statusText}`);
  }
  return payload;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) {
    return "duration pending";
  }
  if (value < 60) {
    return `${value.toFixed(1)}s`;
  }
  return `${Math.floor(value / 60)}m ${(value % 60).toFixed(0)}s`;
}
