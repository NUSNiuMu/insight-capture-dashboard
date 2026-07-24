import { escapeHtml } from "../shared/format.js";

const recordingPanel = document.getElementById("recording-panel");
const recordingStatus = document.getElementById("recording-status");
const storageSpacePill = document.getElementById("storage-space-pill");
const startRecordingButton = document.getElementById("start-recording-button");
const stopRecordingButton = document.getElementById("stop-recording-button");
const refreshRecordTopicsButton = document.getElementById("refresh-record-topics-button");
const recordTopicStatus = document.getElementById("record-topic-status");
const recordTopicGroups = document.getElementById("record-topic-groups");
const recordingOutput = document.getElementById("recording-output");

let recordingBusy = false;
let recordingActive = false;
let recordTopicRefreshBusy = false;
let selectedRecordTopics = new Set();
let knownRecordTopics = new Set();
let recordTopicsInitialized = false;
let recordingLogLines = [];

if (recordingPanel) {
  void refreshRecordingStatus({ refreshTopics: true, force: true });
  window.setInterval(() => {
    void refreshRecordingStatus({ refreshTopics: false });
  }, 1500);
}
if (refreshRecordTopicsButton) {
  refreshRecordTopicsButton.addEventListener("click", () => {
    void refreshRecordTopics({ resetSelection: true });
  });
}
if (startRecordingButton) {
  startRecordingButton.addEventListener("click", () => {
    void startRecording();
  });
}
if (stopRecordingButton) {
  stopRecordingButton.addEventListener("click", () => {
    void stopRecording();
  });
}

window.addEventListener("beforeunload", (event) => {
  if (!recordingActive) return;
  event.preventDefault();
  event.returnValue = "";
});

async function refreshRecordingStatus({ refreshTopics = false, force = false } = {}) {
  if (!recordingPanel) {
    return null;
  }
  if (!force && !recordingPanel.isConnected) {
    return null;
  }
  try {
    const response = await fetch(`/api/recording/status?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to fetch recording status.");
    }
    renderRecordingStatus(payload);
    if (Array.isArray(payload.recent_output) && payload.recent_output.length > 0) {
      replaceRecordingOutput(payload.recent_output.join("\n"));
    }
    if (refreshTopics) {
      await refreshRecordTopics({ resetSelection: !recordTopicsInitialized });
    }
    return payload;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordingOutput(`Recording status error: ${message}`);
    setRecordTopicStatus(message);
    return null;
  }
}

async function refreshRecordTopics({ resetSelection = false } = {}) {
  if (!recordTopicGroups) {
    return null;
  }
  setRecordTopicRefreshBusy(true);
  try {
    const response = await fetch(`/api/recording/topics?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to refresh recording topics.");
    }
    renderTopicCatalog(payload, { resetSelection });
    setRecordTopicStatus(`Topic list refreshed: ${(payload.topics || []).length} topics`);
    return payload;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordTopicStatus(message);
    setRecordingOutput(`Topic refresh error: ${message}`);
    return null;
  } finally {
    setRecordTopicRefreshBusy(false);
  }
}

function renderTopicCatalog(catalog, { resetSelection = false } = {}) {
  if (!recordTopicGroups) {
    return;
  }
  const liveTopics = Array.isArray(catalog && catalog.topics) ? catalog.topics : [];
  const defaultSelectedTopics = Array.isArray(catalog && catalog.default_selected_topics)
    ? catalog.default_selected_topics.filter((topic) => liveTopics.includes(topic))
    : [];
  const previousSelection = new Set(selectedRecordTopics);
  const previousKnown = new Set(knownRecordTopics);
  if (resetSelection || !recordTopicsInitialized || previousKnown.size === 0) {
    selectedRecordTopics = new Set(defaultSelectedTopics);
  } else {
    const mergedSelection = new Set();
    liveTopics.forEach((topic) => {
      if (previousSelection.has(topic) || !previousKnown.has(topic)) {
        mergedSelection.add(topic);
      }
    });
    selectedRecordTopics = mergedSelection;
  }
  knownRecordTopics = new Set(liveTopics);
  recordTopicsInitialized = true;

  const groups = [];
  ((catalog && catalog.cameras) || []).forEach((camera) => {
    groups.push(renderCameraTopicGroup(camera));
  });
  if (Array.isArray(catalog && catalog.other) && catalog.other.length > 0) {
    groups.push(renderCameraTopicGroup({ namespace: "Other", label: "Other", detected: true, topics: catalog.other }));
  }
  recordTopicGroups.innerHTML = groups.length > 0 ? groups.join("") : '<div class="recording-output">No live topics found yet.</div>';
  bindRecordTopicInputs();
  updateRecordTopicSummary();
}

function renderCameraTopicGroup(group) {
  const topics = Array.isArray(group && group.topics) ? group.topics : [];
  const groupKey = escapeHtml((group && (group.namespace || group.label)) || "Other");
  const groupLabel = escapeHtml((group && (group.label || group.namespace)) || "Other");
  const selectedCount = topics.filter((topic) => selectedRecordTopics.has(topic.name)).length;
  return `
    <details class="record-topic-group" open>
      <summary>
        <div class="record-topic-summary">
          <label class="record-topic-select-all">
            <input type="checkbox" data-record-group="${groupKey}" ${selectedCount > 0 ? "checked" : ""}>
            <span class="record-topic-summary-main">
              <strong>${groupLabel}</strong>
              <span class="record-topic-summary-meta">${selectedCount}/${topics.length} selected</span>
            </span>
          </label>
        </div>
      </summary>
      <div class="record-topic-list">
        ${renderTopicList(topics)}
      </div>
    </details>
  `;
}

function renderTopicList(topics) {
  return topics.map((topic) => {
    const checked = selectedRecordTopics.has(topic.name) ? "checked" : "";
    return `
      <label class="record-topic-item">
        <input type="checkbox" data-record-topic value="${escapeHtml(topic.name)}" data-record-group-name="${escapeHtml(topic.group || "")}" ${checked}>
        <span class="record-topic-copy">
          <strong>${escapeHtml(topic.short_name || topic.name)}</strong>
        </span>
      </label>
    `;
  }).join("");
}

function bindRecordTopicInputs() {
  if (!recordTopicGroups) {
    return;
  }
  recordTopicGroups.querySelectorAll("[data-record-topic]").forEach((input) => {
    input.addEventListener("change", (event) => {
      const topic = event.currentTarget.value;
      if (event.currentTarget.checked) {
        selectedRecordTopics.add(topic);
      } else {
        selectedRecordTopics.delete(topic);
      }
      syncRecordGroupStates();
      updateRecordTopicSummary();
    });
  });
  recordTopicGroups.querySelectorAll("[data-record-group]").forEach((input) => {
    input.addEventListener("change", (event) => {
      const group = event.currentTarget.getAttribute("data-record-group");
      const checked = Boolean(event.currentTarget.checked);
      recordTopicGroups.querySelectorAll(`[data-record-group-name="${cssEscape(group)}"]`).forEach((topicInput) => {
        topicInput.checked = checked;
        if (checked) {
          selectedRecordTopics.add(topicInput.value);
        } else {
          selectedRecordTopics.delete(topicInput.value);
        }
      });
      syncRecordGroupStates();
      updateRecordTopicSummary();
    });
  });
  syncRecordGroupStates();
}

function syncRecordGroupStates() {
  if (!recordTopicGroups) {
    return;
  }
  recordTopicGroups.querySelectorAll("[data-record-group]").forEach((input) => {
    const group = input.getAttribute("data-record-group");
    const topicInputs = Array.from(recordTopicGroups.querySelectorAll(`[data-record-group-name="${cssEscape(group)}"]`));
    const checkedCount = topicInputs.filter((item) => item.checked).length;
    input.indeterminate = checkedCount > 0 && checkedCount < topicInputs.length;
    input.checked = topicInputs.length > 0 && checkedCount === topicInputs.length;
  });
}

function updateRecordTopicSummary() {
  const selectedCount = selectedRecordTopics.size;
  const totalCount = knownRecordTopics.size;
  if (totalCount === 0) {
    setRecordTopicStatus("No live topics found. Refresh after ROS topics are available.");
    return;
  }
  setRecordTopicStatus(`${selectedCount}/${totalCount} topics selected`);
}

async function startRecording() {
  if (recordingBusy) {
    return;
  }
  await refreshRecordingStatus({ refreshTopics: false, force: true });
  const topics = collectSelectedRecordTopics();
  if (topics.length === 0) {
    const message = "Select at least one topic to record.";
    setRecordTopicStatus(message);
    setRecordingOutput(message);
    return;
  }
  const bagNameInput = document.getElementById("recording-bag-name");
  const bagName = bagNameInput ? bagNameInput.value.trim() : "";

  setRecordingBusy(true);
  try {
    const body = { topics };
    if (bagName) body.bag_name = bagName;
    const response = await fetch("/api/recording/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to start recording.");
    }
    renderRecordingStatus(payload);
    setRecordingOutput(`Recording started: ${payload.output_path || "(pending path)"}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordingOutput(`Recording start failed: ${message}`);
  } finally {
    setRecordingBusy(false);
  }
}

async function stopRecording() {
  if (recordingBusy) {
    return;
  }
  setRecordingBusy(true);
  try {
    const response = await fetch("/api/recording/stop", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to stop recording.");
    }
    renderRecordingStatus(payload);
    setRecordingOutput("Recording stopped.");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setRecordingOutput(`Recording stop failed: ${message}`);
  } finally {
    setRecordingBusy(false);
  }
}

function collectSelectedRecordTopics() {
  if (!recordTopicGroups) {
    return [];
  }
  const topics = [];
  recordTopicGroups.querySelectorAll("[data-record-topic]").forEach((input) => {
    if (input.checked) {
      topics.push(input.value);
    }
  });
  selectedRecordTopics = new Set(topics);
  return topics;
}

function renderRecordingStatus(status) {
  const active = Boolean(status && status.recording);
  recordingActive = active;
  const outputPath = (status && status.output_path) || "";
  if (recordingStatus) {
    recordingStatus.textContent = active ? `Recording to ${outputPath}` : "Recording idle";
  }
  if (!active && outputPath && recordingOutput && recordingLogLines.length === 0) {
    setRecordingOutput(`Last output: ${outputPath}`);
  }
  if (status && status.topic_catalog && !recordTopicsInitialized) {
    renderTopicCatalog(status.topic_catalog, { resetSelection: true });
  }
  renderDiskSpace(status && status.disk_space);
  setRecordingBusy(recordingBusy, { active });
}

function renderDiskSpace(space) {
  if (!storageSpacePill) {
    return;
  }
  if (!space || typeof space.free_bytes !== "number" || typeof space.free_ratio !== "number") {
    storageSpacePill.textContent = "disk: unknown";
    storageSpacePill.className = "storage-space-pill";
    return;
  }
  const freePercent = space.free_ratio * 100;
  storageSpacePill.textContent = `disk ${formatByteSize(space.free_bytes)} free · ${freePercent.toFixed(0)}%`;
  let level = "ok";
  if (space.free_ratio < 0.1) {
    level = "critical";
  } else if (space.free_ratio < 0.3) {
    level = "warning";
  }
  storageSpacePill.className = `storage-space-pill storage-space-${level}`;
  storageSpacePill.title = `${formatByteSize(space.free_bytes)} free of ${formatByteSize(space.total_bytes)} on the recording filesystem.`;
}

function formatByteSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "--";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const digits = value >= 100 || unitIndex === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unitIndex]}`;
}

function setRecordingBusy(isBusy, { active } = {}) {
  recordingBusy = Boolean(isBusy);
  const isActive = typeof active === "boolean" ? active : Boolean(recordingStatus && recordingStatus.textContent.startsWith("Recording to "));
  if (startRecordingButton) {
    startRecordingButton.disabled = recordingBusy || isActive;
    startRecordingButton.classList.toggle("is-busy", recordingBusy && !isActive);
  }
  if (stopRecordingButton) {
    stopRecordingButton.disabled = recordingBusy || !isActive;
    stopRecordingButton.classList.toggle("is-busy", recordingBusy && isActive);
  }
}

function setRecordTopicRefreshBusy(isBusy) {
  recordTopicRefreshBusy = Boolean(isBusy);
  if (refreshRecordTopicsButton) {
    refreshRecordTopicsButton.disabled = recordTopicRefreshBusy;
    refreshRecordTopicsButton.classList.toggle("is-busy", recordTopicRefreshBusy);
  }
}

function setRecordTopicStatus(message) {
  if (recordTopicStatus) {
    recordTopicStatus.textContent = message;
  }
}

function setRecordingOutput(message) {
  if (!recordingOutput) {
    return;
  }
  const text = String(message || "").trim();
  if (!text) {
    return;
  }
  recordingLogLines.push(text);
  if (recordingLogLines.length > 12) {
    recordingLogLines = recordingLogLines.slice(-12);
  }
  recordingOutput.textContent = recordingLogLines.join("\n");
}

function replaceRecordingOutput(message) {
  if (!recordingOutput) {
    return;
  }
  const text = String(message || "").trim();
  recordingLogLines = text ? text.split("\n").slice(-12) : [];
  recordingOutput.textContent = recordingLogLines.join("\n");
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(String(value));
  }
  return String(value).replaceAll('"', '\\"');
}
