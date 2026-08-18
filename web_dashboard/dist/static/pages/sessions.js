import { escapeHtml } from "../shared/format.js";

const summary = document.getElementById("session-summary");
const list = document.getElementById("take-list");
const status = document.getElementById("take-list-status");
const title = document.getElementById("session-title");
document.getElementById("refresh-sessions-button")?.addEventListener("click", refresh);
void refresh();

async function refresh() {
  try {
    const response = await fetch("/api/sessions", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to load sessions.");
    render(payload);
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
}

function render(payload) {
  const session = payload.session || {};
  const takes = Array.isArray(payload.takes) ? payload.takes : [];
  const accepted = takes.filter((take) => take.operator_valid !== false && take.quick_qc?.state === "pass").length;
  const suspect = takes.filter((take) => take.operator_valid !== false && take.quick_qc?.state === "suspect").length;
  const rejected = takes.filter((take) => take.operator_valid === false).length;
  title.textContent = session.task && session.task !== "unspecified" ? session.task : "Capture sessions";
  summary.innerHTML = [["Session", session.session_id || "--"], ["Takes", takes.length], ["Quick pass", accepted], ["Suspect", suspect], ["Rejected", rejected]].map(([label, value]) => `<article><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`).join("");
  status.textContent = `${takes.length} take${takes.length === 1 ? "" : "s"}`;
  if (!takes.length) {
    list.innerHTML = '<div class="empty-state">No takes recorded in this session yet.</div>';
    return;
  }
  list.innerHTML = takes.map((take) => {
    const qc = take.quick_qc?.state || "pending";
    const outcome = take.operator_valid === false ? "rejected" : qc;
    const anomalyCount = Array.isArray(take.anomaly_timeline) ? take.anomaly_timeline.length : 0;
    const started = take.start_epoch_s ? new Date(take.start_epoch_s * 1000).toLocaleString() : "--";
    return `<article class="take-row take-${escapeHtml(outcome)}"><div><span>TAKE ${String(take.take_id || 0).padStart(4, "0")}</span><strong>${escapeHtml(take.bag_name || "pending bag")}</strong><small>${escapeHtml(started)}</small></div><div><span>Quick QC</span><strong>${escapeHtml(outcome)}</strong><small>${anomalyCount} anomaly event${anomalyCount === 1 ? "" : "s"}</small></div><div><span>Decision</span><strong>${escapeHtml(take.operator_decision || "pending")}</strong><small>${escapeHtml(take.reject_reason || take.state || "")}</small></div></article>`;
  }).join("");
}
