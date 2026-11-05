import { MobilePreview } from "/static/shared/mobile-preview.js?v=20261104-functional-fixes-2";
import { CaptureManagement } from "./management.js?v=20261105-decoupled-mapping";
import { cameraLabel, deviceLabel, errorText, issueText } from "./presentation.js?v=20261105-decoupled-mapping";

const el = id => document.getElementById(id);
const labels = { idle: "Ready", starting: "Starting", recording: "Recording", saving: "Saving", saved: "Saved", error: "Error", playback: "Desktop playback" };
const management = new CaptureManagement(api);
let devices = [];
let previewOptions = "";
const preview = new MobilePreview(el("preview-video"), el("preview-status"));
let snapshot = null, online = false, pending = false, previewEnabled = false, message = "";
let mapping = null;

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(10000), ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(errorText(body.error || `Request failed (${response.status})`));
  return body;
}
function render() {
  const s = snapshot;
  el("connection").textContent = online ? "Connected" : "Reconnecting";
  el("connection").classList.toggle("offline", !online);
  if (!s) return;
  devices = s.devices;
  const head = devices.find(device => device.role === "head");
  el("brand").textContent = head.name.split("_")[0].toUpperCase();
  document.title = "Insight Capture";
  el("device-summary").textContent = devices.map(deviceLabel).join(" / ");
  el("task-name").textContent = s.task_status.active ? `Task: ${s.task_status.task.name}` : "Choose a task to begin";
  el("storage-help").textContent = s.distributed?.enabled
    ? "Head data stays on EgoHalo; wrist data stays on Jetson. Transfer recordings when you finish."
    : "Recordings are saved on Jetson.";
  const options = JSON.stringify(s.preview_cameras);
  if (options !== previewOptions) {
    const selected = el("preview-camera").value;
    el("preview-camera").replaceChildren(...s.preview_cameras.map(camera => new Option(cameraLabel(camera.name), camera.name)));
    if (s.preview_cameras.some(camera => camera.name === selected)) el("preview-camera").value = selected;
    previewOptions = options;
  }
  el("state").textContent = labels[s.state] || s.state;
  el("orb").className = `orb ${s.state}`;
  const help = { idle: "Choose a task and check your devices.", starting: "Starting recording…", recording: "Recording. You can calibrate below.", saving: "Saving your recording. Keep devices powered on.", saved: "Saved. Ready for the next recording.", error: "Check the message below.", playback: "Return to Live on Jetson to resume capture." };
  el("state-help").textContent = online ? help[s.state] : "Connection lost. Recording on Jetson continues.";
  el("storage").textContent = s.storage?.using_fallback ? "Backup disk" : "Capture disk";
  const busy = ["starting", "saving"].includes(s.state);
  const recording = s.state === "recording";
  el("start").hidden = recording;
  el("stop").hidden = !recording;
  el("start").disabled = !online || pending || busy || !s.preflight?.ok;
  el("stop").disabled = !online || pending || s.recording_mode !== "capture";
  const seconds = recording && s.started_at_epoch_s ? Math.max(0, Math.floor(s.server_time - s.started_at_epoch_s)) : 0;
  el("timer").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  if (s.preflight) {
    const free = s.preflight.storage?.free_bytes;
    el("disk").textContent = free != null ? `${(free / 1024 ** 3).toFixed(1)} GB` : "Unknown";
    el("cameras").replaceChildren(...devices.map(device => {
      const {name} = device;
      const h = s.preflight.camera_health?.[name];
      const row = document.createElement("div"); row.className = "camera-row";
      const title = document.createElement("div");
      const text = document.createElement("div"); text.className = "camera-name"; text.textContent = deviceLabel(device);
      const detail = document.createElement("div"); detail.className = "camera-detail";
      detail.textContent = s.state === "playback" ? "Live input paused for desktop playback" : h ? `Video ${h.image_fresh ? "ready" : "missing"} · VIO ${h.vio_fresh ? "ready" : "missing"}` : "Waiting for input";
      title.append(text, detail);
      const badge = document.createElement("span"); badge.className = `camera-status${h?.ready ? "" : " bad"}`;
      badge.classList.toggle("bad", s.state !== "playback" && !h?.ready);
      badge.textContent = s.state === "playback" ? "Paused" : h?.ready ? `${Number(h.image_fps || 0).toFixed(0)} fps` : "Not ready";
      row.append(title, badge); return row;
    }));
    const issues = [...(s.preflight.failures || []), ...(s.preflight.warnings || []),
      ...(s.distributed?.quality_issues || []).map(message => ({ message: `Recording quality: ${message}` }))];
    el("issues").hidden = !issues.length;
    el("issues").replaceChildren(...issues.map(issue => { const p = document.createElement("p"); p.textContent = issueText(issue); return p; }));
  }
  el("filename").textContent = s.output_path || "No recordings yet";
  el("result-state").textContent = labels[s.state] || "—";
  const error = message || s.operation?.error || s.error;
  el("result-error").hidden = !error; el("result-error").textContent = errorText(error) || "";
  renderCalibration();
  management.update(s, pending || !online);
}
function renderCalibration() {
  const headDevice = devices.find(device => device.role === "head");
  el("calibrate").disabled = !online || !mapping || pending
    || ["starting", "saving"].includes(snapshot?.state)
    || (snapshot?.recording && snapshot.recording_mode !== "capture")
    || !headDevice || !snapshot?.preflight?.camera_health?.[headDevice.name]?.ready;
  const statuses = mapping?.statuses || {};
  const head = statuses.insight9 || {};
  const points = Number(mapping?.map_point_count || 0);
  const ready = online && head.online && head.localized && head.clock_ready && head.state === "mapping" && points > 0;
  const rows = [
    ["mapping-head", ready, !online || !head.online ? "Offline" : head.state === "error" ? "Mapping error" : ready ? `${points} map points` : "Waiting for map"],
    ...[["mapping-right", "insight3_a"], ["mapping-left", "insight3_b"]].map(([id, name]) => {
      const s = statuses[name] || {};
      const located = online && s.online && s.localized && s.clock_ready
        && s.map_revision === head.map_revision && s.state !== "error" && s.tracking_mode !== "vio_only";
      const text = !online || !s.online ? "Offline" : s.state === "error" ? "Localization error" : s.tracking_mode === "vio_only" ? "VIO only" : located ? "Localized" : "Not localized";
      return [id, located, text];
    }),
  ];
  for (const [id, good, text] of rows) {
    el(id).textContent = text;
    el(id).classList.toggle("bad", !good);
  }
  el("calibration-summary").textContent = rows.every(([, good]) => good)
    ? "All devices localized" : "Not localized yet. Raw recording is available when devices are ready.";
}
async function poll(path, receive) {
  const value = await api(path()).catch(() => null);
  receive(value);
  setTimeout(() => poll(path, receive), document.hidden ? 4000 : 1500);
}
async function command(action) {
  if (pending || !online) return;
  pending = true; message = ""; render();
  try {
    const commandId = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
    await api(`/api/mobile/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command_id: commandId, output_path: snapshot?.output_path }) });
    snapshot.state = action === "start" ? "starting" : "saving";
  } catch (error) { message = error.message; }
  finally { pending = false; render(); }
}
el("start").onclick = () => command("start");
el("stop").onclick = () => command("stop");
el("calibrate").onclick = async () => {
  if (el("calibrate").disabled) return;
  pending = true; render();
  el("calibration-message").hidden = false;
  el("calibration-message").textContent = "Starting calibration…";
  try {
    const result = await api("/api/mapping/reset", { method: "POST" });
    mapping = result.mapping;
    el("calibration-message").textContent = "Head mapping started. Wrists join when ready. Point devices at a shared textured area.";
  } catch (error) { el("calibration-message").textContent = error.message; }
  finally { pending = false; render(); }
};
function refreshPreview() {
  if (!previewEnabled || document.hidden || snapshot?.state === "playback") {
    preview.stop();
    if (snapshot?.state === "playback") el("preview-status").textContent = "Preview paused for desktop playback.";
    return;
  }
  preview.start(el("preview-camera").value);
}
el("preview-toggle").onclick = () => {
  previewEnabled = !previewEnabled;
  el("preview-panel").hidden = !previewEnabled;
  el("preview-toggle").textContent = previewEnabled ? "Hide preview" : "Show preview";
  refreshPreview();
};
el("preview-camera").onchange = refreshPreview;
document.addEventListener("visibilitychange", refreshPreview);
window.addEventListener("pagehide", () => preview.stop());
poll(() => `/api/mobile/status${previewEnabled && !document.hidden ? "?preview=1" : ""}`, value => {
  const wasPlaying = snapshot?.state === "playback";
  if (value) snapshot = value;
  online = value !== null;
  render();
  if (previewEnabled && online && wasPlaying !== (snapshot.state === "playback")) refreshPreview();
});
poll(() => "/api/mapping", value => { mapping = value; renderCalibration(); });
