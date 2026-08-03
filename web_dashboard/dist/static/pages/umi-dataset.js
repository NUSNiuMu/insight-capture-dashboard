import { escapeHtml, formatDuration } from "../shared/format.js";

const bagList = document.getElementById("umi-bag-list");
const selectAllButton = document.getElementById("umi-select-all");
const cameraLayoutInput = document.getElementById("umi-camera-layout");
const layoutSummary = document.getElementById("umi-layout-summary");
const schemaGrid = document.getElementById("umi-schema-grid");
const imageModeInput = document.getElementById("umi-image-mode");
const exportButton = document.getElementById("umi-export-button");
const statusLabel = document.getElementById("umi-status-label");
const statusDetail = document.getElementById("umi-status-detail");
const progressValue = document.getElementById("umi-progress-value");
const progressFill = document.getElementById("umi-progress-fill");
const resultElement = document.getElementById("umi-result");
let bags = [];
let pollTimer = null;
const CAMERA_LAYOUTS = {
  single_a: {
    label: "Single arm · Insight3 A",
    cameras: ["insight3_a"],
    schema: [["camera0 / robot0", "Right hand · insight3_a · VIO"]],
  },
  single_b: {
    label: "Single arm · Insight3 B",
    cameras: ["insight3_b"],
    schema: [["camera0 / robot0", "Left hand · insight3_b · VIO"]],
  },
  bimanual: {
    label: "Bimanual · A + B + head",
    cameras: ["insight3_a", "insight3_b", "insight9_a"],
    schema: [
      ["camera0 / robot0", "Right hand · insight3_a"],
      ["camera1 / robot1", "Left hand · insight3_b"],
      ["camera2", "Global view · insight9_a"],
    ],
  },
};

function selectedLayout() {
  return CAMERA_LAYOUTS[cameraLayoutInput.value] || CAMERA_LAYOUTS.single_a;
}

function renderLayout() {
  const layout = selectedLayout();
  layoutSummary.textContent = layout.label;
  schemaGrid.innerHTML = layout.schema.map(([key, value]) =>
    `<span><small>${escapeHtml(key)}</small><strong>${escapeHtml(value)}</strong></span>`
  ).join("") + `<span><small>Images</small><strong id="umi-image-summary">${imageModeInput.value === "original" ? "Original" : `${escapeHtml(imageModeInput.value)}×${escapeHtml(imageModeInput.value)}`} RGB · 20 Hz</strong></span><span><small>Gripper / pose</small><strong>Calibrated metres · continuity gated</strong></span><span><small>Output folder</small><strong>outputs/umi_datasets/&lt;rosbag&gt;_umi/</strong></span>`;
}

function formatBytes(bytes) {
  let value = Number(bytes || 0);
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (value < 1024 || unit === "TB") return `${value.toFixed(unit === "B" ? 0 : 1)} ${unit}`;
    value /= 1024;
  }
  return "--";
}

function selectedBags() {
  return Array.from(bagList.querySelectorAll("input[type=checkbox]:checked"), (input) => input.value);
}

function renderBags() {
  if (!bags.length) {
    bagList.innerHTML = '<div class="empty-state">No local rosbags found.</div>';
    return;
  }
  bagList.innerHTML = bags.map((bag) => `
    <label class="umi-bag-option">
      <input type="checkbox" value="${escapeHtml(bag.name || "")}">
      <span><strong>${escapeHtml(bag.name || "unnamed")}</strong><small>${formatDuration(Number(bag.duration_s || 0))} · ${escapeHtml(bag.size_label || "--")} · ${Number(bag.topic_count || 0)} topics</small></span>
    </label>`).join("");
}

async function loadBags() {
  try {
    const response = await fetch(`/api/rosbags?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to load rosbags.");
    bags = Array.isArray(payload.bags) ? payload.bags : [];
    renderBags();
  } catch (error) {
    bagList.innerHTML = `<div class="empty-state">${escapeHtml(error instanceof Error ? error.message : String(error))}</div>`;
  }
}

function setLocked(locked) {
  exportButton.disabled = locked;
  cameraLayoutInput.disabled = locked;
  imageModeInput.disabled = locked;
  selectAllButton.disabled = locked;
  bagList.querySelectorAll("input").forEach((input) => { input.disabled = locked; });
}

function renderStatus(payload) {
  if (payload.status === "idle") return;
  if (payload.status === "running") {
    setLocked(true);
    const count = Number(payload.bag_count || 0);
    const completed = Number(payload.completed_bags || 0);
    const progress = count ? Math.min(95, Math.round((completed / count) * 100)) : 0;
    statusLabel.textContent = payload.stage === "images" ? "Encoding synchronized images" : payload.stage === "package" ? "Packaging Zarr" : "Scanning rosbag streams";
    progressValue.textContent = `${progress}%`;
    progressFill.style.width = `${progress}%`;
    statusDetail.textContent = `${payload.current_bag || "Preparing"}\n${Number(payload.total_frames || 0).toLocaleString()} frames written · ${completed}/${count} bags completed`;
    resultElement.hidden = true;
    return;
  }
  setLocked(false);
  const items = Array.isArray(payload.items) ? payload.items : [];
  const successful = items.filter((item) => item.status === "done");
  const failed = items.filter((item) => item.status === "error");
  statusLabel.textContent = payload.status === "done"
    ? "Outputs saved"
    : payload.status === "partial" ? "Completed with errors" : "Export failed";
  progressValue.textContent = "100%";
  progressFill.classList.toggle("is-error", failed.length > 0);
  progressFill.style.width = "100%";
  statusDetail.textContent = `${successful.length}/${items.length} rosbags saved · ${Number(payload.total_frames || 0).toLocaleString()} total frames${failed.length ? `\n${failed.length} failed; see per-bag details below.` : ""}`;
  resultElement.innerHTML = items.map((item) => {
    const result = item.result || {};
    const isDone = item.status === "done";
    return `<div class="umi-result-item${isDone ? "" : " is-error"}">
      <span><small>Source rosbag</small><strong>${escapeHtml(item.bag_name || "--")}</strong></span>
      <span><small>${isDone ? "Saved on device" : "Export failed"}</small><strong>${escapeHtml(isDone ? item.output_path || "--" : item.error || "Unknown error")}</strong></span>
      <span><small>Summary</small><strong>${isDone ? `${Number(result.total_frames || 0).toLocaleString()} frames · ${formatBytes(result.size_bytes)}` : "No output written"}</strong></span>
    </div>`;
  }).join("");
  resultElement.hidden = false;
}

async function pollStatus() {
  try {
    const response = await fetch(`/api/umi-export/status?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not read export status.");
    renderStatus(payload);
    if (payload.status === "running") pollTimer = window.setTimeout(pollStatus, 800);
  } catch (error) {
    setLocked(false);
    statusLabel.textContent = "Status unavailable";
    statusDetail.textContent = error instanceof Error ? error.message : String(error);
  }
}

selectAllButton.addEventListener("click", () => {
  const inputs = Array.from(bagList.querySelectorAll("input[type=checkbox]"));
  const shouldSelect = inputs.some((input) => !input.checked);
  inputs.forEach((input) => { input.checked = shouldSelect; });
  selectAllButton.textContent = shouldSelect ? "Clear all" : "Select all";
});

imageModeInput.addEventListener("change", () => {
  renderLayout();
});

cameraLayoutInput.addEventListener("change", renderLayout);

exportButton.addEventListener("click", async () => {
  const bagNames = selectedBags();
  if (!bagNames.length) {
    statusLabel.textContent = "Configuration required";
    statusDetail.textContent = "Select at least one rosbag.";
    return;
  }
  window.clearTimeout(pollTimer);
  progressFill.classList.remove("is-error");
  progressFill.style.width = "0%";
  resultElement.hidden = true;
  setLocked(true);
  statusLabel.textContent = "Starting export";
  statusDetail.textContent = `${bagNames.length} bag(s) selected.`;
  try {
    const response = await fetch("/api/umi-export/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bag_names: bagNames,
        image_mode: imageModeInput.value,
        camera_names: selectedLayout().cameras,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not start UMI export.");
    renderStatus(payload);
    pollTimer = window.setTimeout(pollStatus, 300);
  } catch (error) {
    setLocked(false);
    statusLabel.textContent = "Export failed";
    statusDetail.textContent = error instanceof Error ? error.message : String(error);
  }
});

void loadBags();
renderLayout();
void pollStatus();
