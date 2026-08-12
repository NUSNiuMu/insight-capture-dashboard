import { escapeHtml, formatDuration } from "../shared/format.js";

const bagList = document.getElementById("umi-bag-list");
const selectAllButton = document.getElementById("umi-select-all");
const cameraLayoutInput = document.getElementById("umi-camera-layout");
const layoutSummary = document.getElementById("umi-layout-summary");
const schemaGrid = document.getElementById("umi-schema-grid");
const exportFormatInput = document.getElementById("umi-export-format");
const taskField = document.getElementById("umi-task-field");
const taskInput = document.getElementById("umi-task");
const imageModeInput = document.getElementById("umi-image-mode");
const episodeModeInput = document.getElementById("umi-episode-mode");
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
    schema: [["right_wrist_0_rgb / arm 10D", "Insight3 A · absolute EE pose + width"]],
  },
  single_b: {
    label: "Single arm · Insight3 B",
    cameras: ["insight3_b"],
    schema: [["right_wrist_0_rgb / arm 10D", "Insight3 B · absolute EE pose + width"]],
  },
  bimanual: {
    label: "Bimanual · A + B + head",
    cameras: ["insight3_a", "insight3_b", "insight9_a"],
    schema: [
      ["left 10D / left_wrist_0_rgb", "Insight3 B · absolute EE pose + width"],
      ["right 10D / right_wrist_0_rgb", "Insight3 A · absolute EE pose + width"],
      ["base_0_rgb", "Global view · insight9_a"],
    ],
  },
};

function selectedLayout() {
  return CAMERA_LAYOUTS[cameraLayoutInput.value] || CAMERA_LAYOUTS.single_a;
}

function renderLayout() {
  const layout = selectedLayout();
  const isLeRobot = exportFormatInput.value === "lerobot";
  layoutSummary.textContent = layout.label;
  taskField.hidden = !isLeRobot;
  taskInput.required = isLeRobot;
  exportButton.textContent = isLeRobot ? "Inspect and build LeRobot dataset" : "Build UMI outputs";
  schemaGrid.innerHTML = layout.schema.map(([key, value]) =>
    `<span><small>${escapeHtml(key)}</small><strong>${escapeHtml(value)}</strong></span>`
  ).join("") + `<span><small>Images</small><strong id="umi-image-summary">${imageModeInput.value === "original" ? "Original" : `Fixed lower ROI → ${escapeHtml(imageModeInput.value)}×${escapeHtml(imageModeInput.value)}`} RGB · ${isLeRobot ? "20 Hz gripper / 30 Hz hand" : "20 Hz"}</strong></span><span><small>Episodes</small><strong>${episodeModeInput.value === "auto_pause" ? "Auto-split gripper pauses" : "One rosbag = one episode"}</strong></span><span><small>State / action</small><strong>${isLeRobot ? "Auto: UMI EE + width, or inferred hand pose" : "0–0.083 m · relative in training"}</strong></span><span><small>Output folder</small><strong>${isLeRobot ? "outputs/lerobot_datasets/&lt;rosbag&gt;_lerobot/" : "outputs/umi_datasets/&lt;rosbag&gt;_umi/"}</strong></span>`;
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
    bags = Array.isArray(payload.bags)
      ? payload.bags.filter((bag) => !String(bag && bag.name || "").startsWith("fail_"))
      : [];
    renderBags();
  } catch (error) {
    bagList.innerHTML = `<div class="empty-state">${escapeHtml(error instanceof Error ? error.message : String(error))}</div>`;
  }
}

function setLocked(locked) {
  exportButton.disabled = locked;
  exportFormatInput.disabled = locked;
  taskInput.disabled = locked;
  cameraLayoutInput.disabled = locked;
  imageModeInput.disabled = locked;
  episodeModeInput.disabled = locked;
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
    statusLabel.textContent = payload.stage === "detect_route"
      ? "Inspecting gripper QR markers"
      : payload.stage === "hand_inference"
        ? "Inferring human hand pose"
        : payload.stage === "images"
      ? "Encoding synchronized images"
      : payload.stage === "package"
        ? (payload.export_format === "lerobot" ? "Writing Parquet and MP4 metadata" : "Packaging Zarr")
        : payload.stage === "quarantine"
          ? "Marking rejected rosbags"
          : "Scanning rosbag streams";
    progressValue.textContent = `${progress}%`;
    progressFill.style.width = `${progress}%`;
    const route = payload.routes && payload.current_bag ? payload.routes[payload.current_bag] : "";
    const routeText = route === "umi_gripper" ? "UMI gripper route" : route === "ego_hand" ? "Human-hand inference route" : "Detecting route";
    statusDetail.textContent = `${payload.current_bag || "Preparing"} · ${routeText}\n${Number(payload.total_frames || 0).toLocaleString()} frames written · ${completed}/${count} bags completed`;
    resultElement.hidden = true;
    return;
  }
  setLocked(false);
  const items = Array.isArray(payload.items) ? payload.items : [];
  const successful = items.filter((item) => item.status === "done");
  const failed = items.filter((item) => item.status === "error");
  const quarantined = failed.filter((item) => item.source_failed_name);
  statusLabel.textContent = payload.status === "done"
    ? "Outputs saved"
    : payload.status === "partial" ? "Completed with errors" : "Export failed";
  progressValue.textContent = "100%";
  progressFill.classList.toggle("is-error", failed.length > 0);
  progressFill.style.width = "100%";
  statusDetail.textContent = `${successful.length}/${items.length} rosbags saved · ${Number(payload.total_frames || 0).toLocaleString()} total frames${failed.length ? `\n${failed.length} failed; ${quarantined.length} rejected source bag(s) renamed with fail_.` : ""}`;
  resultElement.innerHTML = items.map((item) => {
    const result = item.result || {};
    const isDone = item.status === "done";
    const failedName = item.source_failed_name || "";
    const failureLabel = failedName ? "Rejected rosbag retained" : "Export failed · source retained";
    const failureDetail = item.source_rename_error
      ? `${item.error || "Unknown error"} · rename failed: ${item.source_rename_error}`
      : failedName ? `${item.error || "Unknown error"} · renamed to ${failedName}` : item.error || "Unknown error";
    const routeLabel = item.route === "umi_gripper" ? "UMI gripper" : item.route === "ego_hand" ? "Human hand pose" : "Unknown";
    return `<div class="umi-result-item${isDone ? "" : " is-error"}">
      <span><small>Source rosbag</small><strong>${escapeHtml(item.bag_name || "--")}</strong></span>
      <span><small>${isDone ? `Saved on device · ${routeLabel}` : `${failureLabel} · ${routeLabel}`}</small><strong>${escapeHtml(isDone ? item.output_path || "--" : failureDetail)}</strong></span>
      <span><small>Summary</small><strong>${isDone ? `${Number(result.episode_count || 0)} episode(s) · ${Number(result.total_frames || 0).toLocaleString()} frames · ${formatBytes(result.size_bytes)}` : "No output written"}</strong></span>
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
    else void loadBags();
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

exportFormatInput.addEventListener("change", renderLayout);

episodeModeInput.addEventListener("change", renderLayout);

cameraLayoutInput.addEventListener("change", renderLayout);

exportButton.addEventListener("click", async () => {
  const bagNames = selectedBags();
  if (!bagNames.length) {
    statusLabel.textContent = "Configuration required";
    statusDetail.textContent = "Select at least one rosbag.";
    return;
  }
  if (exportFormatInput.value === "lerobot" && !taskInput.value.trim()) {
    statusLabel.textContent = "Configuration required";
    statusDetail.textContent = "Enter a task instruction for π0.5 language conditioning.";
    taskInput.focus();
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
        episode_mode: episodeModeInput.value,
        camera_names: selectedLayout().cameras,
        export_format: exportFormatInput.value,
        task: taskInput.value.trim(),
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
