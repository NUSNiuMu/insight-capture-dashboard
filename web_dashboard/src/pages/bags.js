import { initializeRosbags } from "../shared/rosbags.js";

initializeRosbags();

const bagSelect = document.getElementById("gripper-bag-select");
const cameraInput = document.getElementById("gripper-camera-name");
const topicInput = document.getElementById("gripper-topic");
const requireCalibration = document.getElementById("gripper-require-calibration");
const runButton = document.getElementById("gripper-extract-button");
const statusElement = document.getElementById("gripper-extraction-status");
const resultElement = document.getElementById("gripper-extraction-result");
let statusTimer = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function setRunning(running) {
  runButton.disabled = running;
  bagSelect.disabled = running;
  cameraInput.disabled = running;
  topicInput.disabled = running;
  requireCalibration.disabled = running;
}

function renderStatus(payload) {
  if (payload.status === "running") {
    setRunning(true);
    statusElement.className = "gripper-extraction-status is-running";
    statusElement.textContent = `Processing ${payload.bag_name} / ${payload.camera_name} · ${Number(payload.processed_frames || 0).toLocaleString()} frames · ${Number(payload.both_detected_frames || 0).toLocaleString()} dual-marker detections`;
    resultElement.hidden = true;
    return;
  }
  setRunning(false);
  if (payload.status === "error") {
    statusElement.className = "gripper-extraction-status is-error";
    statusElement.textContent = payload.error || "Extraction failed.";
    resultElement.hidden = true;
    return;
  }
  if (payload.status !== "done") return;
  const summary = payload.result?.summary || {};
  const calibration = payload.result?.calibration || {};
  const rate = Number(summary.both_detection_rate || 0) * 100;
  statusElement.className = "gripper-extraction-status is-done";
  statusElement.textContent = `Completed ${Number(summary.total_frames || 0).toLocaleString()} frames in ${Number(summary.processing_seconds || 0).toFixed(1)} s.`;
  resultElement.innerHTML = `
    <span><small>Dual detection</small><strong>${rate.toFixed(1)}%</strong></span>
    <span><small>Detected frames</small><strong>${Number(summary.both_detected_frames || 0).toLocaleString()} / ${Number(summary.total_frames || 0).toLocaleString()}</strong></span>
    <span><small>Processing rate</small><strong>${Number(summary.processing_fps || 0).toFixed(1)} FPS</strong></span>
    <span><small>Calibration</small><strong>${calibration.valid ? `${Number(calibration.closed_px).toFixed(2)}–${Number(calibration.open_px).toFixed(2)} px` : "Not applied"}</strong></span>
    <a class="quiet-button" href="${escapeHtml(payload.result_url || "#")}">Download JSON</a>`;
  resultElement.hidden = false;
}

async function pollStatus() {
  try {
    const response = await fetch(`/api/gripper-extraction/status?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not read extraction status.");
    renderStatus(payload);
    if (payload.status === "running") {
      statusTimer = window.setTimeout(pollStatus, 700);
    }
  } catch (error) {
    setRunning(false);
    statusElement.className = "gripper-extraction-status is-error";
    statusElement.textContent = error instanceof Error ? error.message : String(error);
  }
}

runButton.addEventListener("click", async () => {
  const bagName = bagSelect.value;
  const cameraName = cameraInput.value.trim();
  if (!bagName || !cameraName) {
    statusElement.className = "gripper-extraction-status is-error";
    statusElement.textContent = "Select a rosbag and enter a camera name.";
    return;
  }
  window.clearTimeout(statusTimer);
  setRunning(true);
  resultElement.hidden = true;
  statusElement.className = "gripper-extraction-status is-running";
  statusElement.textContent = "Starting extractor...";
  try {
    const response = await fetch("/api/gripper-extraction/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bag_name: bagName,
        camera_name: cameraName,
        topic: topicInput.value.trim(),
        require_calibration: requireCalibration.checked,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not start extraction.");
    renderStatus(payload);
    statusTimer = window.setTimeout(pollStatus, 300);
  } catch (error) {
    setRunning(false);
    statusElement.className = "gripper-extraction-status is-error";
    statusElement.textContent = error instanceof Error ? error.message : String(error);
  }
});

void pollStatus();
