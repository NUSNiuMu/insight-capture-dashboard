const canvas = document.getElementById("render-canvas");
const modelStatus = document.getElementById("model-status");
const legend = document.getElementById("pose-legend");
const cameraDock = document.getElementById("camera-dock");

const ROLE_STYLE = {
  head: { label: "Head", color: "#57d67c", primitive: "sphere", modelColor: "#d6a07d" },
  left_hand: { label: "Left Hand", color: "#4aa8ff", primitive: "box", modelColor: "#c98d6b" },
  right_hand: { label: "Right Hand", color: "#ff6f61", primitive: "box", modelColor: "#c98d6b" }
};

const wsUrl = resolveWebSocketUrl();
const engine = new BABYLON.Engine(canvas, true, { preserveDrawingBuffer: true, stencil: true });
const scene = createScene(engine, canvas);
const poseNodes = new Map();
const modelPromises = new Map();
const modelWarnings = new Set();
const trailStates = new Map();
const cameraPanels = new Map();
const cameraPollState = new Map();
let maximizedCameraName = null;
const RENDER_INTERVAL_MS = 50;
let lastRenderAt = 0;

const TRAIL_TTL_MS = 6000;
const TRAIL_MIN_DISTANCE = 0.02;
const CAMERA_FPS_WINDOW_MS = 1500;
const DEFAULT_TRAIL_ENABLED = {
  head: true,
  left_hand: true,
  right_hand: true
};

engine.runRenderLoop(() => {
  const now = performance.now();
  if ((now - lastRenderAt) < RENDER_INTERVAL_MS) {
    return;
  }
  lastRenderAt = now;
  updateTrails();
  scene.render();
});

window.addEventListener("resize", () => engine.resize());
connect();
startCameraPolling();

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
  modelStatus.textContent = "Connecting pose stream...";
  const ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    modelStatus.textContent = "Pose stream connected";
  };

  ws.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type !== "pose_update") {
      return;
    }
    applyPoseUpdate(payload);
  };

  ws.onerror = () => {
    modelStatus.textContent = "Pose stream error";
  };

  ws.onclose = () => {
    modelStatus.textContent = "Pose stream disconnected, retrying...";
    window.setTimeout(connect, 1000);
  };
}

function startCameraPolling() {
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
  } catch (_error) {
    // The pose WebSocket remains the primary status signal; image polling can retry quietly.
  }
}

async function applyPoseUpdate(payload) {
  const legendRows = [];
  for (const pose of payload.poses || []) {
    const node = ensurePoseNode(pose);
    node.setEnabled(Boolean(pose.visible));
    node.position.copyFromFloats(pose.position[0], pose.position[1], pose.position[2]);
    if (!node.rotationQuaternion) {
      node.rotationQuaternion = new BABYLON.Quaternion();
    }
    node.rotationQuaternion.copyFromFloats(
      pose.quaternion_xyzw[0],
      pose.quaternion_xyzw[1],
      pose.quaternion_xyzw[2],
      pose.quaternion_xyzw[3]
    );
    await ensurePoseVisual(pose, node);
    recordTrailPoint(pose);

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
  legend.innerHTML = legendRows.join("");
  bindTrailToggles();
}

function ensurePoseNode(pose) {
  if (poseNodes.has(pose.name)) {
    return poseNodes.get(pose.name);
  }
  const node = new BABYLON.TransformNode(`pose-${pose.name}`, scene);
  node.rotationQuaternion = new BABYLON.Quaternion(0, 0, 0, 1);
  poseNodes.set(pose.name, node);
  return node;
}

function renderCameraPanels(cameras) {
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
    const instantiated = container.instantiateModelsToScene(() => `${pose.name}-instance`);
    const rootNode = new BABYLON.TransformNode(`${pose.name}-visual`, scene);
    rootNode.parent = node;
    rootNode.scaling = new BABYLON.Vector3(pose.avatar_scale, pose.avatar_scale, pose.avatar_scale);
    instantiated.rootNodes.forEach((child) => {
      child.parent = rootNode;
    });
    collectInstantiatedMeshes(instantiated.rootNodes).forEach((mesh) => {
      mesh.material = createReadableModelMaterial(pose, mesh.material);
      mesh.visibility = 1.0;
      mesh.isPickable = false;
    });
    modelStatus.textContent = `Models: loaded ${modelPath}`;
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
  modelStatus.textContent = `Models: ${reason}`;
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
  const inputs = legend.querySelectorAll('input[data-role]');
  inputs.forEach((input) => {
    input.addEventListener("change", (event) => {
      const role = event.currentTarget.getAttribute("data-role");
      setTrailEnabled(role, event.currentTarget.checked);
    });
  });
}

function recordTrailPoint(pose) {
  const trail = ensureTrailState(pose.role);
  if (!pose.visible || !trail.enabled) {
    return;
  }
  const point = new BABYLON.Vector3(pose.position[0], pose.position[1], pose.position[2]);
  const now = Date.now();
  const lastSample = trail.samples[trail.samples.length - 1];
  if (lastSample && BABYLON.Vector3.Distance(lastSample.point, point) < TRAIL_MIN_DISTANCE) {
    lastSample.point.copyFrom(point);
    lastSample.timestamp = now;
    if (trail.segments.length > 0) {
      const lastSegment = trail.segments[trail.segments.length - 1];
      lastSegment.end.copyFrom(point);
      updateSegmentPoints(lastSegment.mesh, lastSegment.start, lastSegment.end);
    }
    return;
  }

  trail.samples.push({ point, timestamp: now });
  if (trail.samples.length >= 2) {
    const start = trail.samples[trail.samples.length - 2].point.clone();
    const end = trail.samples[trail.samples.length - 1].point.clone();
    const mesh = BABYLON.MeshBuilder.CreateLines(`trail-${pose.role}-${now}`, { points: [start, end] }, scene);
    mesh.color = BABYLON.Color3.FromHexString((ROLE_STYLE[pose.role] || ROLE_STYLE.head).color);
    mesh.alpha = 0.9;
    trail.segments.push({
      mesh,
      start,
      end,
      timestamp: now
    });
  }
}

function updateTrails() {
  const now = Date.now();
  for (const trail of trailStates.values()) {
    if (!trail.enabled) {
      clearTrail(trail);
      continue;
    }

    while (trail.samples.length > 0 && (now - trail.samples[0].timestamp) > TRAIL_TTL_MS) {
      trail.samples.shift();
    }

    while (trail.segments.length > 0) {
      const age = now - trail.segments[0].timestamp;
      if (age <= TRAIL_TTL_MS) {
        break;
      }
      const expired = trail.segments.shift();
      expired.mesh.dispose(false, true);
    }

    trail.segments.forEach((segment) => {
      const age = now - segment.timestamp;
      const fade = Math.max(0.0, 1.0 - age / TRAIL_TTL_MS);
      segment.mesh.alpha = 0.85 * fade;
    });
  }
}

function ensureTrailState(role) {
  if (trailStates.has(role)) {
    return trailStates.get(role);
  }
  const state = {
    role,
    enabled: DEFAULT_TRAIL_ENABLED[role] !== false,
    samples: [],
    segments: []
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
  trail.samples = [];
  trail.segments.forEach((segment) => segment.mesh.dispose(false, true));
  trail.segments = [];
}

function updateSegmentPoints(mesh, start, end) {
  BABYLON.MeshBuilder.CreateLines(null, { points: [start, end], instance: mesh });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
