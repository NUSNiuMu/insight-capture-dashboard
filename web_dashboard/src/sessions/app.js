import { escapeHtml } from "../shared/format.js";

const summary = document.getElementById("session-summary");
const list = document.getElementById("take-list");
const status = document.getElementById("take-list-status");
const title = document.getElementById("session-title");
const taskSetSelect = document.getElementById("task-set-select");
const activateTaskSetButton = document.getElementById("activate-task-set-button");
const editTaskSetButton = document.getElementById("edit-task-set-button");
const newTaskSetButton = document.getElementById("new-task-set-button");
const endTaskSetButton = document.getElementById("end-task-set-button");
const taskSetDetail = document.getElementById("task-set-detail");
const taskManagementStatus = document.getElementById("task-management-status");
const taskEditorDialog = document.getElementById("task-editor-dialog");
const taskEditorForm = document.getElementById("task-editor-form");
const taskEditorTitle = document.getElementById("task-editor-title");
const taskEditorStatus = document.getElementById("task-editor-status");
const taskEditorId = document.getElementById("task-editor-id");
const taskEditorName = document.getElementById("task-editor-name");
const taskEditorSpeechName = document.getElementById("task-editor-speech-name");
const taskEditorCaptureProfile = document.getElementById("task-editor-capture-profile");
const taskEditorInstruction = document.getElementById("task-editor-instruction");
const saveTaskEditorButton = document.getElementById("save-task-editor-button");
let knownTaskSets = [];
let activeTaskId = "";
let activationBusy = false;
let editorBusy = false;
document.getElementById("refresh-sessions-button")?.addEventListener("click", refresh);
activateTaskSetButton?.addEventListener("click", () => { void activateSelectedTaskSet(); });
editTaskSetButton?.addEventListener("click", () => { openTaskEditor(selectedTaskSet()); });
newTaskSetButton?.addEventListener("click", () => { openTaskEditor(); });
endTaskSetButton?.addEventListener("click", () => { void endCurrentTaskSet(); });
taskSetSelect?.addEventListener("change", renderSelectedTaskSetDetail);
taskEditorForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  void saveTaskDefinition();
});
document.getElementById("close-task-editor-button")?.addEventListener("click", () => taskEditorDialog?.close());
document.getElementById("cancel-task-editor-button")?.addEventListener("click", () => taskEditorDialog?.close());
void refresh();

async function refresh({ preferredTaskId = "" } = {}) {
  try {
    const [tasksResponse, sessionsResponse] = await Promise.all([
      fetch("/api/tasks", { cache: "no-store" }),
      fetch("/api/sessions", { cache: "no-store" }),
    ]);
    const [tasksPayload, sessionsPayload] = await Promise.all([
      tasksResponse.json(),
      sessionsResponse.json(),
    ]);
    if (!tasksResponse.ok) throw new Error(tasksPayload.error || "Failed to load task sets.");
    if (!sessionsResponse.ok) throw new Error(sessionsPayload.error || "Failed to load sessions.");
    renderTaskSets(tasksPayload, preferredTaskId);
    render(sessionsPayload);
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
}

function renderTaskSets(payload, preferredTaskId = "") {
  knownTaskSets = Array.isArray(payload.tasks) ? payload.tasks : [];
  activeTaskId = String(payload.active_task_id || "");
  if (!taskSetSelect) return;
  if (!knownTaskSets.length) {
    taskSetSelect.innerHTML = '<option value="">No task sets configured</option>';
    taskSetSelect.disabled = true;
    if (activateTaskSetButton) activateTaskSetButton.disabled = true;
    if (editTaskSetButton) editTaskSetButton.disabled = true;
    if (endTaskSetButton) endTaskSetButton.disabled = true;
    if (taskSetDetail) taskSetDetail.textContent = "Create the first task set to begin capture.";
    return;
  }
  const previous = preferredTaskId || taskSetSelect.value;
  taskSetSelect.innerHTML = knownTaskSets.map((task) => {
    const recorded = Number(task.stats?.recorded_takes || 0);
    return `<option value="${escapeHtml(task.task_id)}">${escapeHtml(task.name)} · ${recorded} take${recorded === 1 ? "" : "s"}</option>`;
  }).join("");
  const preferred = knownTaskSets.some((task) => task.task_id === previous)
    ? previous
    : (activeTaskId || knownTaskSets[0].task_id);
  taskSetSelect.value = preferred;
  taskSetSelect.disabled = false;
  renderSelectedTaskSetDetail();
}

function renderSelectedTaskSetDetail() {
  const selected = selectedTaskSet();
  const isActive = Boolean(selected && selected.task_id === activeTaskId);
  if (activateTaskSetButton) {
    activateTaskSetButton.disabled = activationBusy || !selected || isActive;
    activateTaskSetButton.textContent = isActive ? "Current task set" : "Enter task set";
  }
  if (editTaskSetButton) editTaskSetButton.disabled = activationBusy || !selected;
  if (endTaskSetButton) endTaskSetButton.disabled = activationBusy || !activeTaskId;
  if (taskSetDetail) {
    taskSetDetail.textContent = selected
      ? `${isActive ? "Current" : "Available"} · ${selected.instruction} · raw folder ${selected.recording_subdirectory || selected.task_id}/ · next take ${Number(selected.stats?.next_take_id || 1)}`
      : "No task set selected.";
  }
}

function selectedTaskSet() {
  return knownTaskSets.find((task) => task.task_id === taskSetSelect?.value) || null;
}

async function activateSelectedTaskSet() {
  const taskId = taskSetSelect?.value || "";
  if (!taskId || activationBusy || taskId === activeTaskId) return;
  activationBusy = true;
  renderSelectedTaskSetDetail();
  try {
    const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/activate`, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to enter task set.");
    if (taskManagementStatus) taskManagementStatus.textContent = payload.speech || "Task set entered.";
    await refresh({ preferredTaskId: taskId });
  } catch (error) {
    if (taskSetDetail) taskSetDetail.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    activationBusy = false;
    renderSelectedTaskSetDetail();
  }
}

async function endCurrentTaskSet() {
  if (!activeTaskId || activationBusy) return;
  const active = knownTaskSets.find((task) => task.task_id === activeTaskId);
  if (!window.confirm(`End the current task set “${active?.name || activeTaskId}”? Recorded takes will be preserved.`)) return;
  activationBusy = true;
  renderSelectedTaskSetDetail();
  try {
    const response = await fetch("/api/tasks/current/end", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to end task set.");
    if (taskManagementStatus) taskManagementStatus.textContent = payload.speech || "Task set ended.";
    await refresh({ preferredTaskId: activeTaskId });
  } catch (error) {
    if (taskManagementStatus) taskManagementStatus.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    activationBusy = false;
    renderSelectedTaskSetDetail();
  }
}

function openTaskEditor(task = null) {
  if (!taskEditorDialog || !taskEditorForm || editorBusy) return;
  taskEditorForm.dataset.mode = task ? "edit" : "create";
  taskEditorForm.dataset.taskId = task?.task_id || "";
  if (taskEditorTitle) taskEditorTitle.textContent = task ? "Edit task" : "New task";
  if (saveTaskEditorButton) saveTaskEditorButton.textContent = task ? "Save changes" : "Create task";
  if (taskEditorId) {
    taskEditorId.value = task?.task_id || "";
    taskEditorId.disabled = Boolean(task);
  }
  if (taskEditorName) taskEditorName.value = task?.name || "";
  if (taskEditorSpeechName) taskEditorSpeechName.value = task?.speech_name || "";
  if (taskEditorCaptureProfile) taskEditorCaptureProfile.value = task?.capture_profile || "dual_arm_umi";
  if (taskEditorInstruction) taskEditorInstruction.value = task?.instruction || "";
  if (taskEditorStatus) taskEditorStatus.textContent = task
    ? "Task ID and existing raw folder remain unchanged."
    : "Creating a task does not start recording; enter it after creation.";
  taskEditorDialog.showModal();
  window.setTimeout(() => (task ? taskEditorName : taskEditorId)?.focus(), 0);
}

async function saveTaskDefinition() {
  if (!taskEditorForm || editorBusy || !taskEditorForm.reportValidity()) return;
  const mode = taskEditorForm.dataset.mode || "create";
  const existingId = taskEditorForm.dataset.taskId || "";
  const taskId = mode === "edit" ? existingId : taskEditorId?.value.trim() || "";
  const payload = {
    id: taskId,
    name: taskEditorName?.value.trim() || "",
    speech_name: taskEditorSpeechName?.value.trim() || "",
    instruction: taskEditorInstruction?.value.trim() || "",
    capture_profile: taskEditorCaptureProfile?.value.trim() || ""
  };
  editorBusy = true;
  if (saveTaskEditorButton) saveTaskEditorButton.disabled = true;
  try {
    const response = await fetch(mode === "edit" ? `/api/tasks/${encodeURIComponent(taskId)}` : "/api/tasks", {
      method: mode === "edit" ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Failed to save task.");
    taskEditorDialog?.close();
    if (taskManagementStatus) taskManagementStatus.textContent = mode === "edit"
      ? `Updated ${result.task.name}.`
      : `Created ${result.task.name}. Select “Enter task set” before recording.`;
    await refresh({ preferredTaskId: result.task.task_id });
  } catch (error) {
    if (taskEditorStatus) taskEditorStatus.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    editorBusy = false;
    if (saveTaskEditorButton) saveTaskEditorButton.disabled = false;
  }
}

function render(payload) {
  const session = payload.session || {};
  const takes = Array.isArray(payload.takes) ? payload.takes : [];
  const stats = payload.stats || {};
  const accepted = takes.filter((take) => take.operator_valid !== false && take.quick_qc?.state === "pass").length;
  const suspect = takes.filter((take) => take.operator_valid !== false && take.quick_qc?.state === "suspect").length;
  const rejected = takes.filter((take) => take.operator_valid === false).length;
  title.textContent = session.active === false
    ? "No active task set"
    : (session.task_name || session.task || "Task sets");
  summary.innerHTML = [["Task set", session.task_name || "--"], ["Task set ID", session.session_id || "--"], ["Folder", session.recording_subdirectory || "--"], ["Recorded", Number(stats.recorded_takes || 0)], ["Next take", Number(stats.next_take_id || 1)], ["Quick pass", accepted], ["Suspect", suspect], ["Rejected", rejected]].map(([label, value]) => `<article><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`).join("");
  status.textContent = `${Number(stats.recorded_takes || 0)} recorded take${Number(stats.recorded_takes || 0) === 1 ? "" : "s"}`;
  if (!takes.length) {
    list.innerHTML = '<div class="empty-state">No takes recorded in this task set yet.</div>';
    return;
  }
  list.innerHTML = takes.map((take) => {
    const qc = take.quick_qc?.state || "pending";
    const outcome = take.operator_valid === false ? "rejected" : qc;
    const anomalyCount = Array.isArray(take.anomaly_timeline) ? take.anomaly_timeline.length : 0;
    const started = take.start_epoch_s ? new Date(take.start_epoch_s * 1000).toLocaleString() : "--";
    const openAction = take.bag_id
      ? `<a class="take-open-link" href="/3d?bag=${encodeURIComponent(take.bag_id)}">Open rosbag</a>`
      : '<small>Rosbag is not available in the library.</small>';
    return `<article class="take-row take-${escapeHtml(outcome)}"><div><span>TAKE ${String(take.take_id || 0).padStart(4, "0")}</span><strong>${escapeHtml(take.bag_name || "pending bag")}</strong><small>${escapeHtml(started)}</small></div><div><span>Quick QC</span><strong>${escapeHtml(outcome)}</strong><small>${anomalyCount} anomaly event${anomalyCount === 1 ? "" : "s"}</small></div><div><span>Decision</span><strong>${escapeHtml(take.operator_decision || "pending")}</strong><small>${escapeHtml(take.reject_reason || take.state || "")}</small>${openAction}</div></article>`;
  }).join("");
}
