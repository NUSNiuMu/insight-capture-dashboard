import { MobilePreview } from "/static/shared/mobile-preview.js?v=20261027-capture-cleanup";

const el = id => document.getElementById(id);
const labels = { idle: "准备采集", starting: "正在启动", recording: "采集中", saving: "正在保存", saved: "保存完成", error: "采集异常" };
const names = { insight9_a: "头部 · Insight9", insight3_a: "右手 · Insight3 A", insight3_b: "左手 · Insight3 B" };
const preview = new MobilePreview(el("preview-video"), el("preview-status"));
let snapshot = null, online = false, pending = false, previewEnabled = false, message = "";
let mapping = null;

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(10000), ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.speech || body.error || `请求失败 (${response.status})`);
  return body;
}
function render() {
  const s = snapshot;
  el("connection").textContent = online ? "Jetson 已连接" : "连接中断 · 正在重连";
  el("connection").classList.toggle("offline", !online);
  if (!s) return;
  el("state").textContent = labels[s.state] || s.state;
  el("orb").className = `orb ${s.state}`;
  const help = { idle: "设备就绪后即可开始。", starting: "正在启动录制，请稍候。", recording: "数据正在写入 Jetson。", saving: "正在收尾写盘，请等待保存完成后再断电。", saved: "这一条已保存，可以开始下一条。", error: "请查看下方原因，确认后再试。" };
  el("state-help").textContent = online ? help[s.state] : "控制连接已断开，Jetson 上的采集不会因此停止。";
  el("storage").textContent = s.storage?.using_fallback ? "备用存储" : "采集盘";
  const busy = ["starting", "saving"].includes(s.state);
  const recording = s.state === "recording";
  el("start").hidden = recording;
  el("stop").hidden = !recording;
  el("start").disabled = !online || pending || busy || !s.preflight?.ok;
  el("stop").disabled = !online || pending || s.recording_mode !== "mobile_capture";
  const seconds = recording && s.started_at_epoch_s ? Math.max(0, Math.floor(s.server_time - s.started_at_epoch_s)) : 0;
  el("timer").textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  if (s.preflight) {
    const free = s.preflight.storage?.free_bytes;
    el("disk").textContent = free != null ? `${(free / 1024 ** 3).toFixed(1)} GB` : "未知";
    el("cameras").replaceChildren(...Object.entries(names).map(([name, label]) => {
      const h = s.preflight.camera_health?.[name];
      const row = document.createElement("div"); row.className = "camera-row";
      const title = document.createElement("div");
      const text = document.createElement("div"); text.className = "camera-name"; text.textContent = label;
      const detail = document.createElement("div"); detail.className = "camera-detail";
      detail.textContent = h ? `图像 ${h.image_fresh ? "正常" : "缺失"} · VIO ${h.vio_fresh ? "正常" : "缺失"}` : "等待输入";
      title.append(text, detail);
      const badge = document.createElement("span"); badge.className = `camera-status${h?.ready ? "" : " bad"}`;
      badge.textContent = h?.ready ? `${Number(h.image_fps || 0).toFixed(0)} fps` : "未就绪";
      row.append(title, badge); return row;
    }));
    const issues = [...(s.preflight.failures || []), ...(s.preflight.warnings || [])];
    el("issues").hidden = !issues.length;
    el("issues").replaceChildren(...issues.map(issue => { const p = document.createElement("p"); p.textContent = issue.message; return p; }));
  }
  el("filename").textContent = s.output_path || "尚未采集";
  el("result-state").textContent = labels[s.state] || "—";
  const error = message || s.operation?.error || s.error;
  el("result-error").hidden = !error; el("result-error").textContent = error || "";
  renderCalibration();
}
function renderCalibration() {
  el("calibrate").disabled = !online || !mapping || pending
    || ["starting", "recording", "saving"].includes(snapshot?.state)
    || !Object.keys(names).every(name => snapshot?.preflight?.camera_health?.[name]?.ready);
  const statuses = mapping?.statuses || {};
  const head = statuses.insight9 || {};
  const points = Number(mapping?.map_point_count || 0);
  const ready = online && head.online && head.state === "mapping" && points > 0;
  const rows = [
    ["mapping-head", ready, !online || !head.online ? "服务离线" : head.state === "error" ? "建图异常" : ready ? `已建图 · ${points} 点` : "等待建图"],
    ...[["mapping-right", "insight3_a"], ["mapping-left", "insight3_b"]].map(([id, name]) => {
      const s = statuses[name] || {};
      const located = online && s.online && s.localized && s.state !== "error" && s.tracking_mode !== "vio_only";
      const text = !online || !s.online ? "服务离线" : s.state === "error" ? "定位异常" : s.tracking_mode === "vio_only" ? "VIO 推算 · 未匹配地图" : located ? "已定位" : "未定位";
      return [id, located, text];
    }),
  ];
  for (const [id, good, text] of rows) {
    el(id).textContent = text;
    el(id).classList.toggle("bad", !good);
  }
  el("calibration-summary").textContent = rows.every(([, good]) => good)
    ? "三路全局定位就绪" : "统一坐标尚未就绪；原始数据仍可按设备状态采集。";
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
  el("calibration-message").textContent = "正在请求校准…";
  try {
    const result = await api("/api/mapping/reset", { method: "POST" });
    mapping = result.mapping;
    el("calibration-message").textContent = "校准已请求。请缓慢移动头部观察环境，并让双手相机看到已建图区域，等待三路定位就绪。";
  } catch (error) { el("calibration-message").textContent = error.message; }
  finally { pending = false; render(); }
};
el("preview-toggle").onclick = () => {
  previewEnabled = !previewEnabled;
  el("preview-panel").hidden = !previewEnabled;
  el("preview-toggle").textContent = previewEnabled ? "关闭预览" : "开启预览";
  if (previewEnabled) preview.start(el("preview-camera").value); else preview.stop();
};
el("preview-camera").onchange = () => { if (previewEnabled) preview.start(el("preview-camera").value); };
document.addEventListener("visibilitychange", () => {
  if (document.hidden) preview.stop(); else if (previewEnabled) preview.start(el("preview-camera").value);
});
window.addEventListener("pagehide", () => preview.stop());
poll(() => `/api/mobile/status${previewEnabled && !document.hidden ? "?preview=1" : ""}`, value => {
  if (value) snapshot = value;
  online = value !== null;
  render();
});
poll(() => "/api/mapping", value => { mapping = value; renderCalibration(); });
