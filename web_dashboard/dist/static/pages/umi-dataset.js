import { escapeHtml, formatDuration } from "../shared/format.js";

const bagList = document.getElementById("umi-bag-list");
const selectAllButton = document.getElementById("umi-select-all");
const datasetNameInput = document.getElementById("umi-dataset-name");
const imageModeInput = document.getElementById("umi-image-mode");
const imageSummary = document.getElementById("umi-image-summary");
const exportButton = document.getElementById("umi-export-button");
const statusLabel = document.getElementById("umi-status-label");
const statusDetail = document.getElementById("umi-status-detail");
const progressValue = document.getElementById("umi-progress-value");
const progressFill = document.getElementById("umi-progress-fill");
const resultElement = document.getElementById("umi-result");
let bags = [];
let pollTimer = null;

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
  datasetNameInput.disabled = locked;
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
  if (payload.status === "error") {
    statusLabel.textContent = "Export failed";
    progressValue.textContent = "ERROR";
    progressFill.classList.add("is-error");
    statusDetail.textContent = payload.error || "Unknown export error.";
    resultElement.hidden = true;
    return;
  }
  const result = payload.result || {};
  statusLabel.textContent = "Dataset ready";
  progressValue.textContent = "100%";
  progressFill.classList.remove("is-error");
  progressFill.style.width = "100%";
  const sizes = Object.entries(result.camera_image_sizes || {}).map(([name, size]) => `${name} ${size[0]}×${size[1]}`).join(" · ");
  statusDetail.textContent = `${Number(result.episode_count || 0)} episodes · ${Number(result.total_frames || 0).toLocaleString()} frames · ${formatDuration(Number(result.duration_s || 0))}\nCamera order: ${(result.camera_order || []).join(" · ")}\nImages: ${sizes || "--"}`;
  resultElement.innerHTML = `
    <div><small>Zarr archive</small><strong>${formatBytes(result.size_bytes)}</strong></div>
    <div><small>Episodes</small><strong>${Number(result.episode_count || 0)}</strong></div>
    <div><small>Frames</small><strong>${Number(result.total_frames || 0).toLocaleString()}</strong></div>
    <a class="primary-button" href="${escapeHtml(payload.result_url || "#")}">Download Zarr</a>
    <a class="quiet-button" href="${escapeHtml(payload.config_url || "#")}">Training config</a>
    <a class="quiet-button" href="${escapeHtml(payload.manifest_url || "#")}">Manifest</a>`;
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
  imageSummary.textContent = imageModeInput.value === "original"
    ? "Original RGB · 20 Hz"
    : `${imageModeInput.value}×${imageModeInput.value} RGB · 20 Hz`;
});

exportButton.addEventListener("click", async () => {
  const bagNames = selectedBags();
  const datasetName = datasetNameInput.value.trim();
  if (!bagNames.length || !datasetName) {
    statusLabel.textContent = "Configuration required";
    statusDetail.textContent = "Select at least one rosbag and enter a dataset name.";
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
      body: JSON.stringify({ dataset_name: datasetName, bag_names: bagNames, image_mode: imageModeInput.value }),
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
void pollStatus();
