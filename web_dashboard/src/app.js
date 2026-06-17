const dashboardView = document.body.dataset.dashboardView || "full";
const enable3d = dashboardView === "full" || dashboardView === "3d";
const enableCameras = dashboardView === "full" || dashboardView === "cameras";

const canvas = document.getElementById("render-canvas");
const modelStatus = document.getElementById("model-status");
const mappingVersion = document.getElementById("mapping-version");
const legend = document.getElementById("pose-legend");
const trailWidthSlider = document.getElementById("trail-width-slider");
const trailWidthValue = document.getElementById("trail-width-value");
const cameraDock = document.getElementById("camera-dock");
const cameraPageMeta = document.getElementById("camera-page-meta");

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
const TRAIL_SMOOTHING_ALPHA = 0.28;
const TRAIL_TESSELLATION = 20;
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
if (engine) {
  engine.setHardwareScalingLevel(Math.min(window.devicePixelRatio || 1, 1.5) > 1 ? 1 / Math.min(window.devicePixelRatio || 1, 1.5) : 1);
}
const poseNodes = new Map();
const modelPromises = new Map();
const modelWarnings = new Set();
const trailStates = new Map();
const cameraPanels = new Map();
const cameraPollState = new Map();
let maximizedCameraName = null;
let legendMarkupCache = "";

const CAMERA_FPS_WINDOW_MS = 1500;
const DEFAULT_TRAIL_ENABLED = {
  head: true,
  left_hand: true,
  right_hand: true
};
const TRAIL_WIDTH_STORAGE_KEY = "insight-trail-width-multiplier";
let trailWidthMultiplier = loadTrailWidthMultiplier();
const SCENE_MAPPING_VERSION = "RUF-v3";
let manualGripperOpenRatio = null;

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
  initializeGripperKeyboardControls();
  connect();
}
if (enableCameras) {
  startCameraPolling();
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
  sceneRef.getEngine().setHardwareScalingLevel(Math.min(window.devicePixelRatio || 1, 1.5) > 1 ? 1 / Math.min(window.devicePixelRatio || 1, 1.5) : 1);

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
    if (payload.type !== "pose_update") {
      return;
    }
    applyPoseUpdate(payload).catch((error) => {
      warnOnce("pose-update-error", `Pose update failed: ${error?.stack || error}`);
    });
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
  window.setInterval(pollCameraMetadata, 100);
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
    renderCameraPanels(payload.cameras || []);
    if (cameraPageMeta) {
      const liveCount = (payload.cameras || []).filter((camera) => !camera.stale && camera.visible).length;
      cameraPageMeta.textContent = `${liveCount}/${(payload.cameras || []).length} streams live`;
    }
  } catch (_error) {
    // The pose WebSocket remains the primary status signal; image polling can retry quietly.
  }
}

async function applyPoseUpdate(payload) {
  if (!enable3d || !scene) {
    return;
  }
  const legendRows = [];
  for (const pose of payload.poses || []) {
    const node = ensurePoseNode(pose);
    if (!node) {
      continue;
    }
    node.metadata = { ...(node.metadata || {}), poseRole: pose.role };
    ensureNodeTransformFields(node);
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

    const style = ROLE_STYLE[pose.role] || { label: pose.role, color: "#cccccc" };
    legendRows.push(
      `<div class="legend-row">
        <div class="legend-main">
          <span><span class="swatch" style="background:${style.color}"></span><strong>${style.label}</strong></span>
          <span class="legend-meta">${pose.visible ? pose.name : pose.name + " hidden"}</span>
        </div>
        <label class="trail-toggle">
          <span>Trail</span>
          <input type="checkbox" data-role="${escapeHtml(pose.role)}" ${isTrailEnabled(pose.role) ? "checked" : ""}>
        </label>
      </div>`
    );
  }
  renderPoseLegend(legendRows);
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

function ensureNodeTransformFields(node) {
  if (!node.position || typeof node.position.copyFromFloats !== "function") {
    node.position = new BABYLON.Vector3(0, 0, 0);
  }
  if (!node.rotationQuaternion || typeof node.rotationQuaternion.copyFromFloats !== "function") {
    node.rotationQuaternion = new BABYLON.Quaternion(0, 0, 0, 1);
  }
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
    status.textContent = camera.stale ? "stale" : camera.visible ? "live" : "waiting";
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
    if (!fingerNode || !fingerNode.position || typeof fingerNode.position.copyFrom !== "function" || !base) {
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
    leftBasePositions: leftFingerNodes.map((node) => (node.position ? node.position.clone() : null)),
    rightBasePositions: rightFingerNodes.map((node) => (node.position ? node.position.clone() : null)),
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
  const handleTrailToggle = (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || !target.matches("input[data-role]")) {
      return;
    }
    const role = target.getAttribute("data-role");
    setTrailEnabled(role, target.checked);
    target.checked = isTrailEnabled(role);
    legendMarkupCache = "";
  };
  legend.addEventListener("click", handleTrailToggle);
  legend.addEventListener("change", handleTrailToggle);
  legend.addEventListener("input", handleTrailToggle);
}

function renderPoseLegend(legendRows) {
  if (!legend) {
    return;
  }
  const nextMarkup = legendRows.join("");
  if (nextMarkup !== legendMarkupCache) {
    legend.innerHTML = nextMarkup;
    legendMarkupCache = nextMarkup;
  }
  bindTrailToggles();
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
    return;
  }
  refreshAllTrails();
}

function isTrailEnabled(role) {
  return ensureTrailState(role).enabled;
}

function clearTrail(trail) {
  trail.points = [];
  if (trail.mesh) {
    trail.mesh.dispose(false, true);
    trail.mesh = null;
  }
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

function updateTrailWidthLabel() {
  if (trailWidthValue) {
    trailWidthValue.textContent = `${trailWidthMultiplier.toFixed(1)}x`;
  }
}

function refreshAllTrails() {
  for (const trail of trailStates.values()) {
    if (trail.points.length >= 2) {
      refreshTrailMesh(trail);
    }
  }
}

function updateTrailFromPose(pose) {
  const trail = ensureTrailState(pose.role);
  if (!trail.enabled) {
    clearTrail(trail);
    return;
  }
  const sourcePoints = smoothTrailPoints((pose.trace || []).map((sample) => mapDashboardPositionToScene(sample)));
  if (sourcePoints.length < 2) {
    return;
  }
  const firstPoint = sourcePoints[0];
  const hasMotion = sourcePoints.some((point) => BABYLON.Vector3.Distance(point, firstPoint) > 0.02);
  if (!hasMotion) {
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
  const radius = (TRAIL_RADIUS_BY_ROLE[trail.role] || 0.016) * trailWidthMultiplier;
  const previousMaterial = trail.mesh ? trail.mesh.material : null;
  if (trail.mesh) {
    trail.mesh.dispose(false, false);
  }
  trail.mesh = BABYLON.MeshBuilder.CreateTube(
    `trail-${trail.role}`,
    { path: points, radius, tessellation: TRAIL_TESSELLATION, updatable: false },
    scene
  );
  trail.mesh.isPickable = false;
  trail.mesh.alwaysSelectAsActiveMesh = true;
  trail.mesh.renderingGroupId = 1;
  if (previousMaterial) {
    trail.mesh.material = previousMaterial;
  }
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

function smoothTrailPoints(points) {
  if (points.length <= 2) {
    return points;
  }
  const smoothed = [points[0].clone()];
  for (let index = 1; index < points.length - 1; index += 1) {
    const previous = smoothed[smoothed.length - 1];
    const current = points[index];
    smoothed.push(new BABYLON.Vector3(
      previous.x + (current.x - previous.x) * TRAIL_SMOOTHING_ALPHA,
      previous.y + (current.y - previous.y) * TRAIL_SMOOTHING_ALPHA,
      previous.z + (current.z - previous.z) * TRAIL_SMOOTHING_ALPHA
    ));
  }
  smoothed.push(points[points.length - 1].clone());
  return smoothed;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
