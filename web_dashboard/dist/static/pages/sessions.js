import { escapeHtml } from "../shared/format.js";

const summary = document.getElementById("session-summary");
const list = document.getElementById("take-list");
const status = document.getElementById("take-list-status");
const title = document.getElementById("session-title");
const taskSetSelect = document.getElementById("task-set-select");
const activateTaskSetButton = document.getElementById("activate-task-set-button");
const taskSetDetail = document.getElementById("task-set-detail");
let knownTaskSets = [];
let activeTaskId = "";
let activationBusy = false;
document.getElementById("refresh-sessions-button")?.addEventListener("click", refresh);
activateTaskSetButton?.addEventListener("click", () => { void activateSelectedTaskSet(); });
taskSetSelect?.addEventListener("change", renderSelectedTaskSetDetail);
void refresh();

async function refresh() {
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
    renderTaskSets(tasksPayload);
    render(sessionsPayload);
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
}

function renderTaskSets(payload) {
  knownTaskSets = Array.isArray(payload.tasks) ? payload.tasks : [];
  activeTaskId = String(payload.active_task_id || "");
  if (!taskSetSelect) return;
  if (!knownTaskSets.length) {
    taskSetSelect.innerHTML = '<option value="">No task sets configured</option>';
    taskSetSelect.disabled = true;
    if (activateTaskSetButton) activateTaskSetButton.disabled = true;
    if (taskSetDetail) taskSetDetail.textContent = "Add task definitions in config/capture_tasks.json.";
    return;
  }
  const previous = taskSetSelect.value;
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
  const selected = knownTaskSets.find((task) => task.task_id === taskSetSelect?.value);
  const isActive = Boolean(selected && selected.task_id === activeTaskId);
  if (activateTaskSetButton) {
    activateTaskSetButton.disabled = activationBusy || !selected || isActive;
    activateTaskSetButton.textContent = isActive ? "Current task set" : "Enter task set";
  }
  if (taskSetDetail) {
    taskSetDetail.textContent = selected
      ? `${selected.instruction} · raw folder ${selected.recording_subdirectory || selected.task_id}/ · next take ${Number(selected.stats?.next_take_id || 1)}`
      : "No task set selected.";
  }
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
    await refresh();
  } catch (error) {
    if (taskSetDetail) taskSetDetail.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    activationBusy = false;
    renderSelectedTaskSetDetail();
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
