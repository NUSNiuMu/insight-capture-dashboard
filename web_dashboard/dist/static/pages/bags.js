import { initializeRosbags } from "../shared/rosbags.js?v=20260818-bag-library";

initializeRosbags();

const panel = document.getElementById("deferred-recordings");
const message = document.getElementById("deferred-message");
let submitting = false;

async function refreshDeferred() {
  try {
    const response = await fetch("/api/postprocessing/recordings", { cache: "no-store" });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error);
    panel.hidden = !status.enabled;
    const list = document.getElementById("deferred-list");
    list.replaceChildren();
    const labels = { saved: "原始数据已保存 · 待合包", copying: "正在回传并校验", merging: "正在合包", done: "已合包", error: "合包失败 · 原始数据保留" };
    for (const recording of status.recordings) {
      const row = document.createElement("article"); row.className = "bag-card";
      const title = document.createElement("h3");
      title.textContent = `${recording.task_name} · 第 ${recording.take_id} 条`;
      const detail = document.createElement("p");
      detail.textContent = `${new Date(recording.created_at_epoch_s * 1000).toLocaleString()} · ${labels[recording.phase] || recording.phase}`;
      row.append(title, detail);
      if (recording.error) {
        const error = document.createElement("p"); error.textContent = recording.error; row.append(error);
      }
      if (["saved", "error"].includes(recording.phase)) {
        const button = document.createElement("button"); button.className = "quiet-button";
        button.textContent = "回传并合包"; button.disabled = status.busy || submitting;
        button.onclick = async () => {
          submitting = true; button.disabled = true;
          try {
            const response = await fetch("/api/postprocessing/merge", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: recording.session_id }) });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error);
            message.textContent = "已开始回传并合包，请等待完成。";
          } catch (error) { message.textContent = error.message; }
          finally { submitting = false; await refreshDeferred(); }
        };
        row.append(button);
      }
      list.append(row);
    }
    if (!status.recordings.length) list.textContent = "暂无已保存的双端录制。";
  } catch (error) { message.textContent = error.message; }
}
document.getElementById("refresh-deferred").onclick = refreshDeferred;
refreshDeferred();
setInterval(refreshDeferred, 5000);
