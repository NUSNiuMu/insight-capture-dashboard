import { escapeHtml } from "../shared/format.js";
import { initializeRosbags, refreshRosbags } from "../shared/rosbags.js";

const optimizationCameraSelect = document.getElementById("optimization-camera-group");
const optimizationRunNameInput = document.getElementById("optimization-run-name");
const startOptimizationButton = document.getElementById("start-optimization-button");
const stopOptimizationButton = document.getElementById("stop-optimization-button");
const optimizationStepLabel = document.getElementById("optimization-step-label");
const optimizationLogEl = document.getElementById("optimization-log");
const optimizationResultPanel = document.getElementById("optimization-result-panel");
const optimizationLogLink = document.getElementById("optimization-log-link");
let optimizationBusy = false;
let optimizationPollTimer = null;

initializeRosbags();
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
  const bagSelect = document.getElementById("optimization-bag-select");
  if (bagSelect) {
    bagSelect.addEventListener("change", updateAutoRunName);
  }
}

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
  scene.clearColor = new BABYLON.Color4(0.937, 0.906, 0.843, 1);

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

  // Matches the CSS plot-legend swatches (--red / --green) on warm paper.
  drawLine(vioNorm, new BABYLON.Color3(0.76, 0.27, 0.24));
  drawLine(colmapNorm, new BABYLON.Color3(0.12, 0.54, 0.33));

  const span = allPts.length > 0 ? Math.max(...allPts.map((p) => Math.abs(p[0] - c[0])), ...allPts.map((p) => Math.abs(p[2] - c[2]))) : 1;
  camera.radius = Math.max(span * 2.5, 0.5);

  engine.runRenderLoop(() => scene.render());
  window.addEventListener("resize", () => {
    canvas.width = canvas.offsetWidth;
    canvas.height = canvas.offsetHeight;
    engine.resize();
  });
}
