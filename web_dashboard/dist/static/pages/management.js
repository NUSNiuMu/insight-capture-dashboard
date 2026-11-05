import { errorText } from "./presentation.js?v=20261105-decoupled-mapping";

const el = id => document.getElementById(id);
const phaseLabels = { saved: "Saved · transfer pending", copying: "Transferring", merging: "Merging", done: "Ready", error: "Transfer failed · originals kept" };

export class CaptureManagement {
  constructor(api) {
    this.api = api;
    this.busy = false;
    this.captureBusy = true;
    this.status = null;
    this.panel = el("management");
    this.panel.ontoggle = () => { if (this.panel.open) this.refresh(); };
    el("management-refresh").onclick = () => this.refresh();
    el("save-diagnostics").onclick = async () => {
      try {
        const snapshot = await this.api("/api/diagnostics");
        snapshot.client = { user_agent: navigator.userAgent };
        const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = "insight-diagnostics.json";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        el("management-message").textContent = "Diagnostics saved.";
      } catch (error) { this.showError(error); }
    };
    el("task-activate").onclick = () => this.run(() => this.post(`/api/tasks/${encodeURIComponent(el("task-select").value)}/activate`));
    el("task-end").onclick = () => this.run(() => this.post("/api/tasks/current/end"));
    el("task-create").onsubmit = event => {
      event.preventDefault();
      this.run(async () => {
        const id = "task_" + Array.from(crypto.getRandomValues(new Uint8Array(8)), byte => byte.toString(16).padStart(2, "0")).join("");
        const { task } = await this.post("/api/tasks", { id, name: el("task-new-name").value.trim(), instruction: el("task-new-instruction").value.trim(), capture_profile: "dual_arm_umi" });
        await this.post(`/api/tasks/${encodeURIComponent(task.task_id)}/activate`);
        el("task-create").reset();
        el("task-select").value = "";
      });
    };
    this.timer = setInterval(() => {
      if (this.panel.open && !document.hidden && !this.busy) this.refreshRecordings().catch(error => this.showError(error));
    }, 5000);
    window.addEventListener("pagehide", () => clearInterval(this.timer));
  }
  post(path, body = {}) {
    return this.api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  }
  showError(error) { el("management-message").textContent = error.message; }
  update(status, pending) {
    this.captureBusy = pending || status.recording || ["starting", "saving"].includes(status.state);
    const disabled = this.busy || this.captureBusy;
    for (const id of ["task-select", "task-new-name", "task-new-instruction", "task-create-button", "task-activate", "task-end"])
      el(id).disabled = disabled;
    el("task-activate").disabled ||= !el("task-select").value;
    el("task-end").disabled ||= !status.task_status?.active;
    for (const button of el("saved-recordings").querySelectorAll("button"))
      button.disabled = disabled || this.status?.busy;
  }
  async run(action) {
    if (this.busy || this.captureBusy) return;
    this.busy = true;
    el("management-message").textContent = "Working…";
    try {
      await action();
      await this.load();
      el("management-message").textContent = "Done.";
    } catch (error) { this.showError(error); }
    finally { this.busy = false; }
  }
  async refresh() {
    try { await this.load(); }
    catch (error) { this.showError(error); }
  }
  async load() {
    const [tasks] = await Promise.all([this.api("/api/tasks"), this.refreshRecordings()]);
    const select = el("task-select");
    const selected = select.value || tasks.active_task_id;
    select.replaceChildren(...tasks.tasks.map(task => new Option(task.name, task.task_id)));
    if (tasks.tasks.some(task => task.task_id === selected)) select.value = selected;
  }
  async refreshRecordings() {
    this.status = await this.api("/api/postprocessing/recordings");
    const list = el("saved-recordings");
    list.replaceChildren();
    if (!this.status.enabled || !this.status.recordings.length) {
      list.textContent = this.status.enabled ? "No recordings to transfer." : "Recordings are saved locally. No transfer needed.";
      return;
    }
    for (const recording of this.status.recordings) {
      const row = document.createElement("article"); row.className = "saved-recording";
      const title = document.createElement("p");
      title.textContent = `${recording.task_name} · Take ${recording.take_id} · ${phaseLabels[recording.phase] || recording.phase}`;
      row.append(title);
      if (recording.error) { const detail = document.createElement("p"); detail.textContent = errorText(recording.error); row.append(detail); }
      if (["saved", "error"].includes(recording.phase)) {
        const button = document.createElement("button"); button.type = "button"; button.textContent = "Transfer & merge";
        button.disabled = this.busy || this.captureBusy || this.status.busy;
        button.onclick = () => this.run(() => this.post("/api/postprocessing/merge", { session_id: recording.session_id }));
        row.append(button);
      }
      list.append(row);
    }
  }
}
