import { escapeHtml } from "../shared/format.js";
import { applyTraceUpdate } from "./trace-buffer.js";

const enable3d = true;
const canvas = document.getElementById("render-canvas");
const modelStatus = document.getElementById("model-status");
const legend = document.getElementById("pose-legend");
const ROLE_STYLE = {
  head: { label: "Head", color: "#79c47b", primitive: "sphere", modelColor: "#b99572" },
  left_hand: { label: "Left Hand", color: "#79adc2", primitive: "box", modelColor: "#9f8569" },
  right_hand: { label: "Right Hand", color: "#cf7f6f", primitive: "box", modelColor: "#9f8569" }
};
const TRAIL_SCREEN_WIDTH_BY_ROLE = {
  head: 0.012,
  left_hand: 0.01,
  right_hand: 0.01
};
const HAND_RIG_EDGES = [
  [0, 1, "thumb"], [1, 2, "thumb"], [2, 3, "thumb"], [3, 4, "thumb"],
  [0, 5, "palm"], [5, 6, "index"], [6, 7, "index"], [7, 8, "index"],
  [5, 9, "palm"], [9, 10, "middle"], [10, 11, "middle"], [11, 12, "middle"],
  [9, 13, "palm"], [13, 14, "ring"], [14, 15, "ring"], [15, 16, "ring"],
  [13, 17, "palm"], [0, 17, "palm"], [17, 18, "pinky"], [18, 19, "pinky"], [19, 20, "pinky"]
];
const HAND_RIG_SCALE = 0.09;
const HAND_RIG_COLOR = "#ff0000";
const HAND_RIG_RADIUS = 0.004;
const DEFAULT_TRAIL_ENABLED = {
  head: true,
  left_hand: true,
  right_hand: true
};

const engine = canvas ? new BABYLON.Engine(canvas, true, { preserveDrawingBuffer: false, stencil: true }) : null;
const scene = engine && canvas ? createScene(engine, canvas) : null;
const poseNodes = new Map();
const modelPromises = new Map();
const modelWarnings = new Set();
const trailStates = new Map();
const handRigs = new Map();
const keptPoints = new Map();
const traceCaches = new Map();
const pendingTrailPoses = new Map();
let pendingPosePayload = null;
let keepTrajectory = false;
let stickFigureMode = false;
let sceneFrameIntervalMs = 1000 / 20;
let traceCapacity = 300;

if (engine && scene) {
  let sceneRenderBudgetMs = 0;
  engine.setHardwareScalingLevel(1.4);
  engine.runRenderLoop(() => {
    sceneRenderBudgetMs += engine.getDeltaTime();
    if (sceneRenderBudgetMs < sceneFrameIntervalMs) return;
    sceneRenderBudgetMs %= sceneFrameIntervalMs;
    flushPendingPoseUpdate();
    flushPendingTrailUpdates();
    updateTrails();
    scene.render();
  });
  window.addEventListener("resize", () => engine.resize());
}

export function setKeepTrajectory(enabled) {
  keepTrajectory = Boolean(enabled);
  if (!keepTrajectory) keptPoints.clear();
}

export function clearKeptTrajectory() {
  keptPoints.clear();
}

export function clearRenderedTrajectories() {
  keptPoints.clear();
  traceCaches.clear();
  pendingPosePayload = null;
  pendingTrailPoses.clear();
  for (const trail of trailStates.values()) clearTrail(trail);
}

export function stopSpatialRenderer() {
  if (engine) engine.stopRenderLoop();
}

function createScene(engineRef, canvasRef) {
  const sceneRef = new BABYLON.Scene(engineRef);
  // Warm paper, a step deeper than the page background so the viewport
  // reads as a surface (Daylight Telemetry theme).
  sceneRef.clearColor = new BABYLON.Color4(0.937, 0.906, 0.843, 1.0);

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

  const gridSize = 8;
  const gridCells = 20;
  const gridHalf = gridSize / 2;
  const gridStep = gridSize / gridCells;
  const gridLines = [];
  for (let index = 0; index <= gridCells; index += 1) {
    const offset = -gridHalf + index * gridStep;
    gridLines.push([
      new BABYLON.Vector3(-gridHalf, 0, offset),
      new BABYLON.Vector3(gridHalf, 0, offset),
    ]);
    gridLines.push([
      new BABYLON.Vector3(offset, 0, -gridHalf),
      new BABYLON.Vector3(offset, 0, gridHalf),
    ]);
  }
  const grid = BABYLON.MeshBuilder.CreateLineSystem("grid", { lines: gridLines }, sceneRef);
  grid.color = new BABYLON.Color3(0.72, 0.67, 0.58);
  grid.alpha = 0.55;
  grid.isPickable = false;

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

export function queuePoseUpdate(payload) {
  setDisplayFpsLimit(payload.display_fps_limit);
  const configuredCapacity = Number(payload.trace_capacity);
  if (Number.isInteger(configuredCapacity) && configuredCapacity >= 2) {
    traceCapacity = configuredCapacity;
  }
  let traceStateValid = true;
  for (const pose of payload.poses || []) {
    traceStateValid = applyTraceUpdate(
      traceCaches,
      pose,
      traceCapacity,
      mapDashboardPositionToScene
    ) && traceStateValid;
  }
  pendingPosePayload = payload;
  return traceStateValid;
}

function applyPoseUpdate(payload) {
  if (!enable3d || !scene) {
    return;
  }
  // Toggled from the Settings page; buildAssetKey folds it in, so flipping
  // it makes ensurePoseVisual dispose the GLB/marker and rebuild the other.
  stickFigureMode = Boolean(payload.stick_figure_mode);
  // Trace deltas are accumulated in queuePoseUpdate while hidden; rendering
  // resumes from the latest pose and the complete local trace cache.
  if (document.hidden) {
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

  for (const pose of poses) {
    const node = ensurePoseNode(pose);
    if (!node) continue;
    if (!node.position) node.position = new BABYLON.Vector3(0, 0, 0);
    if (!node.rotationQuaternion) node.rotationQuaternion = new BABYLON.Quaternion(0, 0, 0, 1);
    const position = Array.isArray(pose.position) ? pose.position : [0, 0, 0];
    const quaternion = Array.isArray(pose.quaternion_xyzw) ? pose.quaternion_xyzw : [0, 0, 0, 1];
    const scenePosition = mapDashboardPositionToScene(position);
    const sceneQuaternion = mapDashboardQuaternionToScene(quaternion);
    node.setEnabled(Boolean(pose.visible));
    node.position.copyFromFloats(scenePosition.x, scenePosition.y, scenePosition.z);
    node.rotationQuaternion.copyFromFloats(sceneQuaternion.x, sceneQuaternion.y, sceneQuaternion.z, sceneQuaternion.w);
    pendingTrailPoses.set(pose.role, {
      pose,
      tracePoints: traceCaches.get(pose.role)?.points || [],
    });
    if (!node.metadata || node.metadata.assetKey !== buildAssetKey(pose)) {
      void ensurePoseVisual(pose, node).then(() => {
        applyGripperOpening(pose, node);
      });
    } else {
      applyGripperOpening(pose, node);
    }
    updateHandRig(pose, node);
    if (legend) {
      const row = legend.querySelector(`[data-legend-role="${CSS.escape(pose.role)}"] .legend-meta`);
      if (row) row.textContent = pose.visible ? pose.name : `${pose.name} hidden`;
    }
  }
}

function flushPendingPoseUpdate() {
  if (!pendingPosePayload || document.hidden) {
    return;
  }
  const payload = pendingPosePayload;
  pendingPosePayload = null;
  applyPoseUpdate(payload);
}

function setDisplayFpsLimit(value) {
  const fps = Number(value);
  if (Number.isFinite(fps) && fps > 0) {
    sceneFrameIntervalMs = 1000 / Math.min(120, fps);
  }
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


async function ensurePoseVisual(pose, node) {
  if (!scene) {
    return;
  }
  if (node.metadata && node.metadata.assetKey === buildAssetKey(pose)) {
    return;
  }

  disposeNodeChildren(node);
  node.metadata = { assetKey: buildAssetKey(pose) };

  if (stickFigureMode) {
    attachStickMarker(pose, node);
    return;
  }

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
    // Preserve glTF node names for gripper-finger lookup.
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

function attachStickMarker(pose, node) {
  // Stick-figure mode: one large role-colored dot per pose instead of the
  // GLB avatar, so the skeleton lines carry the picture. Fixed size on
  // purpose -- avatar_scale belongs to the models this mode replaces.
  const style = ROLE_STYLE[pose.role] || ROLE_STYLE.head;
  const color = BABYLON.Color3.FromHexString(style.color);
  const material = new BABYLON.StandardMaterial(`stick-marker-mat-${pose.name}`, scene);
  material.diffuseColor = color;
  material.emissiveColor = color.scale(0.55);
  material.specularColor = BABYLON.Color3.Black();
  const mesh = BABYLON.MeshBuilder.CreateSphere(
    `stick-marker-${pose.name}`,
    { diameter: pose.role === "head" ? 0.06 : 0.0225 },
    scene
  );
  mesh.material = material;
  mesh.parent = node;
  mesh.isPickable = false;
  if (modelStatus) {
    modelStatus.textContent = "Models: stick-figure markers";
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
  // Stop at pad contact instead of moving both finger centroids to X=0.
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

function handLandmarkToLocal(landmark) {
  // Map [along, lateral, normal] to this rig's empirically aligned local axes.
  return new BABYLON.Vector3(
    landmark[0] * HAND_RIG_SCALE,
    landmark[2] * HAND_RIG_SCALE,
    landmark[1] * HAND_RIG_SCALE
  );
}

let handRigMaterial = null;

function ensureHandRigMaterial() {
  // Shared by every bone tube across both hands -- one material, not one
  // per mesh. Unlit (disableLighting) so the red reads the same regardless
  // of scene lighting/angle, matching how a plain colored line would look.
  if (!handRigMaterial && scene) {
    handRigMaterial = new BABYLON.StandardMaterial("hand-rig-mat", scene);
    const color = BABYLON.Color3.FromHexString(HAND_RIG_COLOR);
    handRigMaterial.diffuseColor = color;
    handRigMaterial.emissiveColor = color;
    handRigMaterial.specularColor = BABYLON.Color3.Black();
    handRigMaterial.disableLighting = true;
  }
  return handRigMaterial;
}

function updateHandRig(pose, node) {
  if (!scene) {
    return;
  }
  let rig = handRigs.get(pose.name);
  // ensurePoseVisual's disposeNodeChildren wipes every descendant of the
  // pose node when the avatar model changes -- including these parented
  // bone tubes -- so recreate rather than instance-update a disposed mesh.
  if (rig && rig.tubes.some((tube) => tube && tube.isDisposed())) {
    handRigs.delete(pose.name);
    rig = null;
  }
  const landmarks = Array.isArray(pose.hand_landmarks) ? pose.hand_landmarks : null;
  if (!stickFigureMode || !pose.visible || !landmarks) {
    if (rig) rig.tubes.forEach((tube) => tube && tube.setEnabled(false));
    return;
  }
  if (!rig) {
    rig = { tubes: new Array(HAND_RIG_EDGES.length).fill(null) };
    handRigs.set(pose.name, rig);
  }
  const material = ensureHandRigMaterial();
  HAND_RIG_EDGES.forEach(([a, b], index) => {
    const pointA = landmarks[a];
    const pointB = landmarks[b];
    const tube = rig.tubes[index];
    if (!pointA || !pointB) {
      if (tube) tube.setEnabled(false);
      return;
    }
    const path = [handLandmarkToLocal(pointA), handLandmarkToLocal(pointB)];
    if (tube) {
      BABYLON.MeshBuilder.CreateTube(null, { path, instance: tube });
      tube.setEnabled(true);
    } else {
      const mesh = BABYLON.MeshBuilder.CreateTube(
        `hand-rig-${pose.name}-${index}`,
        { path, radius: HAND_RIG_RADIUS, tessellation: 6, updatable: true, cap: BABYLON.Mesh.CAP_ALL },
        scene
      );
      mesh.material = material;
      mesh.parent = node;
      mesh.isPickable = false;
      mesh.renderingGroupId = 1;
      rig.tubes[index] = mesh;
    }
  });
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
  if (stickFigureMode) {
    return `stick-marker:${pose.role}`;
  }
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

function flushPendingTrailUpdates() {
  if (pendingTrailPoses.size === 0) {
    return;
  }
  const updates = Array.from(pendingTrailPoses.values());
  pendingTrailPoses.clear();
  updates.forEach(({ pose, tracePoints }) => updateTrailFromPose(pose, tracePoints));
}

function ensureTrailState(role) {
  if (trailStates.has(role)) {
    return trailStates.get(role);
  }
  const state = {
    role,
    enabled: DEFAULT_TRAIL_ENABLED[role] !== false,
    points: [],
    mesh: null,
    meshCapacity: 0,
    basePoints: [],
    offsets: null
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
  trail.meshCapacity = 0;
  trail.basePoints = [];
  trail.offsets = null;
  if (trail.mesh) {
    trail.mesh.dispose(false, true);
    trail.mesh = null;
  }
}

function updateTrailFromPose(pose, tracePoints) {
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
  const sourcePoints = tracePoints;
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
  const screenWidth = TRAIL_SCREEN_WIDTH_BY_ROLE[trail.role] || 0.005;
  const capacity = keepTrajectory
    ? Math.max(traceCapacity, Math.ceil(trail.points.length / traceCapacity) * traceCapacity)
    : traceCapacity;
  const points = resampleTrailPoints(trail.points, capacity);
  if (trail.mesh && trail.meshCapacity !== capacity) {
    trail.mesh.dispose(false, true);
    trail.mesh = null;
    trail.basePoints = [];
    trail.offsets = null;
  }
  if (!trail.mesh) {
    trail.basePoints = points.map((point) => point.clone());
    const flattenedPoints = trail.basePoints.flatMap((point) => [point.x, point.y, point.z]);
    trail.mesh = BABYLON.CreateGreasedLine(
      `trail-${trail.role}`,
      { points: flattenedPoints, updatable: true },
      {
        width: screenWidth,
        sizeAttenuation: false,
        color: roleColor,
      },
      scene
    );
    trail.mesh.isPickable = false;
    trail.mesh.alwaysSelectAsActiveMesh = true;
    trail.mesh.renderingGroupId = 1;
    trail.meshCapacity = capacity;
    trail.offsets = new Float32Array(capacity * 6);
  } else {
    const offsets = trail.offsets;
    for (let index = 0; index < capacity; index += 1) {
      const point = points[index];
      const basePoint = trail.basePoints[index];
      const offsetIndex = index * 6;
      const offsetX = point.x - basePoint.x;
      const offsetY = point.y - basePoint.y;
      const offsetZ = point.z - basePoint.z;
      offsets[offsetIndex] = offsetX;
      offsets[offsetIndex + 1] = offsetY;
      offsets[offsetIndex + 2] = offsetZ;
      offsets[offsetIndex + 3] = offsetX;
      offsets[offsetIndex + 4] = offsetY;
      offsets[offsetIndex + 5] = offsetZ;
    }
    trail.mesh.offsets = offsets;
  }
  if (trail.mesh.material) {
    trail.mesh.material.alpha = 0.96;
    trail.mesh.material.backFaceCulling = false;
    trail.mesh.material.twoSidedLighting = true;
  }
}

function resampleTrailPoints(points, targetCount) {
  const compacted = [points[0]];
  for (let index = 1; index < points.length; index += 1) {
    if (BABYLON.Vector3.DistanceSquared(points[index], compacted.at(-1)) > 1e-8) {
      compacted.push(points[index]);
    }
  }
  if (compacted.length < 2) {
    return Array.from({ length: targetCount }, () => points[0].clone());
  }

  const cumulativeDistances = [0];
  for (let index = 1; index < compacted.length; index += 1) {
    cumulativeDistances.push(
      cumulativeDistances[index - 1]
      + BABYLON.Vector3.Distance(compacted[index - 1], compacted[index])
    );
  }
  const totalDistance = cumulativeDistances.at(-1);
  const resampled = new Array(targetCount);
  let segmentIndex = 1;
  for (let index = 0; index < targetCount; index += 1) {
    const targetDistance = totalDistance * index / (targetCount - 1);
    while (
      segmentIndex < cumulativeDistances.length - 1
      && cumulativeDistances[segmentIndex] < targetDistance
    ) {
      segmentIndex += 1;
    }
    const segmentStartDistance = cumulativeDistances[segmentIndex - 1];
    const segmentLength = cumulativeDistances[segmentIndex] - segmentStartDistance;
    const amount = segmentLength > 0
      ? (targetDistance - segmentStartDistance) / segmentLength
      : 0;
    resampled[index] = BABYLON.Vector3.Lerp(
      compacted[segmentIndex - 1],
      compacted[segmentIndex],
      amount
    );
  }
  return resampled;
}
