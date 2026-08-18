import { createHandPoseViewer } from "../handpose/viewer.js?v=20260729-wilor-only";
import { escapeHtml } from "../shared/format.js";
import { initializeRosbags } from "../shared/rosbags.js?v=20260812-review-bundle-v1";

const elements = {
  bag: document.getElementById("handpose-bag-select"),
  capability: document.getElementById("handpose-capability-help"),
  methodChip: document.getElementById("handpose-method-chip"),
  jobChip: document.getElementById("handpose-job-chip"),
  start: document.getElementById("start-handpose-button"),
  stop: document.getElementById("stop-handpose-button"),
  status: document.getElementById("handpose-status-label"),
  progress: document.getElementById("handpose-progress-label"),
  progressFill: document.getElementById("handpose-progress-fill"),
  log: document.getElementById("handpose-log"),
  resultTitle: document.getElementById("handpose-result-title"),
  resultSelect: document.getElementById("handpose-result-select"),
  loadResult: document.getElementById("load-handpose-result-button"),
  preview: document.getElementById("handpose-preview-link"),
};

const HANDPOSE_METHOD = "wilor";

const viewer = createHandPoseViewer({
  canvas: document.getElementById("handpose-canvas"),
  empty: document.getElementById("handpose-empty"),
  timeline: document.getElementById("handpose-timeline"),
  playButton: document.getElementById("handpose-play-button"),
  timeLabel: document.getElementById("handpose-time-label"),
  coordinateLabel: document.getElementById("handpose-coordinate-label"),
});

let capabilities = null;
let lastLoadedUrl = "";
let autoLoadedResultUrl = "";
let pollTimer = 0;

function selectedCapability() {
  return capabilities?.methods?.[HANDPOSE_METHOD] || null;
}

function renderCapability() {
  const capability = selectedCapability();
  if (!capability) {
    elements.capability.textContent = "Runtime information is unavailable.";
    elements.methodChip.textContent = "Runtime unknown";
    elements.start.disabled = true;
    return;
  }
  if (capability.available) {
    elements.capability.textContent = "Camera-space coordinates · ready";
    elements.methodChip.textContent = "WiLoR ready";
    elements.start.disabled = false;
  } else {
    elements.capability.textContent = `Unavailable: ${capability.missing.join(", ")}`;
    elements.methodChip.textContent = "WiLoR unavailable";
    elements.start.disabled = true;
  }
}

async function fetchJson(url, options) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

async function refreshCapabilities() {
  try {
    capabilities = await fetchJson("/api/handpose/capabilities");
    renderCapability();
  } catch (error) {
    elements.capability.textContent = `Runtime check failed: ${error.message}`;
    elements.methodChip.textContent = "Runtime error";
    elements.start.disabled = true;
  }
}

function renderResults(results) {
  const previous = elements.resultSelect.value;
  const entries = Array.isArray(results) ? results : [];
  if (!entries.length) {
    elements.resultSelect.innerHTML = '<option value="">No saved results</option>';
    elements.loadResult.disabled = true;
    return;
  }
  elements.resultSelect.innerHTML = entries.map((entry) => {
    const value = encodeURIComponent(JSON.stringify(entry));
    return `<option value="${value}">${escapeHtml(entry.bag_name)} · ${escapeHtml(entry.method)}</option>`;
  }).join("");
  if (previous && Array.from(elements.resultSelect.options).some((option) => option.value === previous)) {
    elements.resultSelect.value = previous;
  }
  elements.loadResult.disabled = false;
}

function selectedResult() {
  if (!elements.resultSelect.value) return null;
  try {
    return JSON.parse(decodeURIComponent(elements.resultSelect.value));
  } catch {
    return null;
  }
}

async function loadResult(entry, force = false) {
  if (!entry?.result_url || (!force && entry.result_url === lastLoadedUrl)) return;
  elements.resultTitle.textContent = `Loading ${entry.bag_name} · ${entry.method}`;
  const records = await fetchJson(`${entry.result_url}&ts=${Date.now()}`);
  viewer.setFrames(records);
  elements.resultTitle.textContent = `${entry.bag_name} · ${entry.method}`;
  elements.preview.hidden = !entry.preview_ready;
  if (entry.preview_ready) elements.preview.href = entry.preview_url;
  lastLoadedUrl = entry.result_url;
}

function renderStatus(payload) {
  const running = payload.state === "running";
  const processed = Math.max(0, Number(payload.processed_frames || 0));
  const detected = Math.max(0, Number(payload.detected_frames || 0));
  const total = Math.max(0, Number(payload.total_frames || 0));
  const percent = total > 0
    ? Math.min(100, (processed / total) * 100)
    : (payload.state === "done" ? 100 : 0);
  elements.start.hidden = running;
  elements.stop.hidden = !running;
  elements.jobChip.textContent = payload.state || "idle";
  elements.status.textContent = running ? `Extracting ${payload.bag_name} with ${payload.method}` : (payload.error ? `Error: ${payload.error}` : payload.state || "Idle");
  const frameProgress = total > 0
    ? `${processed.toLocaleString()} / ${total.toLocaleString()} frames`
    : `${processed.toLocaleString()} frames`;
  elements.progress.textContent = `${frameProgress} · ${percent.toFixed(1)}% · ${detected.toLocaleString()} detected`;
  elements.progressFill.style.width = `${percent}%`;
  elements.log.textContent = (payload.log_tail || []).join("\n") || "Waiting for a task.";
  elements.log.scrollTop = elements.log.scrollHeight;
  renderResults(payload.results);
  if (running) autoLoadedResultUrl = "";
  if (
    payload.state === "done"
    && payload.result_ready
    && payload.result_url !== autoLoadedResultUrl
  ) {
    autoLoadedResultUrl = payload.result_url;
    void loadResult({
      bag_name: payload.bag_name,
      method: payload.method,
      result_url: payload.result_url,
      preview_ready: payload.preview_ready,
      preview_url: payload.preview_url,
    });
  }
}

async function refreshStatus() {
  try {
    renderStatus(await fetchJson("/api/handpose/status"));
  } catch (error) {
    elements.jobChip.textContent = "status error";
    elements.status.textContent = error.message;
  }
}

async function startExtraction() {
  if (!elements.bag.value) {
    elements.status.textContent = "Select a rosbag first.";
    return;
  }
  elements.start.disabled = true;
  try {
    renderStatus(await fetchJson("/api/handpose/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bag_name: elements.bag.value, method: HANDPOSE_METHOD }),
    }));
  } catch (error) {
    elements.status.textContent = `Error: ${error.message}`;
    renderCapability();
  }
}

async function stopExtraction() {
  elements.stop.disabled = true;
  try {
    renderStatus(await fetchJson("/api/handpose/stop", { method: "POST" }));
  } catch (error) {
    elements.status.textContent = `Error: ${error.message}`;
  } finally {
    elements.stop.disabled = false;
  }
}

initializeRosbags();
elements.start.addEventListener("click", () => { void startExtraction(); });
elements.stop.addEventListener("click", () => { void stopExtraction(); });
elements.loadResult.addEventListener("click", () => {
  const entry = selectedResult();
  if (entry) void loadResult(entry, true).catch((error) => { elements.status.textContent = `Error: ${error.message}`; });
});
void refreshCapabilities();
void refreshStatus();
pollTimer = window.setInterval(() => { void refreshStatus(); }, 1500);
window.addEventListener("pagehide", () => window.clearInterval(pollTimer));
