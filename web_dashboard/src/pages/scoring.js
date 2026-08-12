import { escapeHtml } from "../shared/format.js";
import { initializeRosbags, refreshRosbags } from "../shared/rosbags.js?v=20260812-review-bundle-v1";

const runScoringButton = document.getElementById("run-scoring-button");
const integrityResultEl = document.getElementById("integrity-result");
const integrityResultBody = document.getElementById("integrity-result-body");
const scoringTopicInput = document.getElementById("scoring-topic");
const scoringStatusEyebrow = document.getElementById("scoring-status-eyebrow");
const scoringStatusEl = document.getElementById("scoring-status");
const scoringResultEl = document.getElementById("scoring-result");
const scoringResultBody = document.getElementById("scoring-result-body");
let scoringBusy = false;
let scoringPollTimer = null;

initializeRosbags();
if (runScoringButton) {
  void pollScoringStatus();
  runScoringButton.addEventListener("click", () => {
    void runScoringAndVerify();
  });
}

async function runScoringAndVerify() {
  // Single "Scoring" button: run the exact integrity check first (its report
  // stays visible), then kick off the scoring job regardless of the verdict.
  if (scoringBusy) {
    return;
  }
  const bagSelect = document.getElementById("scoring-bag-select");
  const bagName = bagSelect ? bagSelect.value : "";
  if (!bagName) {
    setScoringStatus("Select a rosbag first.");
    return;
  }
  if (runScoringButton) {
    runScoringButton.disabled = true;
  }
  await runIntegrityCheck();
  await runScoring();
}

async function runScoring() {
  if (scoringBusy) {
    return;
  }
  const bagSelect = document.getElementById("scoring-bag-select");
  const bagName = bagSelect ? bagSelect.value : "";
  if (!bagName) {
    setScoringStatus("Select a rosbag first.");
    return;
  }
  const topic = scoringTopicInput ? scoringTopicInput.value.trim() : "";

  scoringBusy = true;
  if (runScoringButton) {
    runScoringButton.disabled = true;
  }
  hideScoringResult();
  setScoringStatus("Starting...");

  try {
    const body = { bag_name: bagName };
    if (topic) {
      body.topic = topic;
    }
    const response = await fetch("/api/scoring/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) {
      setScoringStatus(`Error: ${payload.error || "Failed to start scoring."}`);
      scoringBusy = false;
      if (runScoringButton) {
        runScoringButton.disabled = false;
      }
      return;
    }
    setScoringStatus("Running... (this may take a minute)");
    scheduleScoringPoll(1500);
  } catch (error) {
    setScoringStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
    scoringBusy = false;
    if (runScoringButton) {
      runScoringButton.disabled = false;
    }
  }
}

async function runIntegrityCheck() {
  const bagSelect = document.getElementById("scoring-bag-select");
  const bagName = bagSelect ? bagSelect.value : "";
  if (!bagName) {
    setScoringStatus("Select a rosbag first.");
    return;
  }
  hideIntegrityResult();
  setScoringStatus(`Verifying integrity of ${bagName}...`);
  try {
    const response = await fetch("/api/integrity/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bag_name: bagName }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setScoringStatus(`Integrity error: ${payload.error || response.statusText}`);
      return;
    }
    const scope = payload.scope === "image_streams" ? "image streams" : "all topics";
    setScoringStatus(payload.ok
      ? `Integrity OK: ${bagName} — ${scope} complete`
      : `Integrity FAILED: ${bagName} — ${payload.failed_topics.length} topic(s) with frame loss`);
    renderIntegrityResult(payload);
    void refreshRosbags(); // update the Bags-page badge data
  } catch (error) {
    setScoringStatus(`Integrity error: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function hideIntegrityResult() {
  if (integrityResultEl) {
    integrityResultEl.hidden = true;
  }
}

function renderIntegrityResult(report) {
  if (!integrityResultEl || !integrityResultBody) {
    return;
  }
  const okColor = "#57d67c";
  const badColor = "#ff5a5a";
  const topics = Array.isArray(report.topics) ? report.topics : [];
  const rows = topics.map((topic) => {
    const color = topic.ok ? okColor : badColor;
    const detail = topic.error
      ? escapeHtml(topic.error)
      : `${Number(topic.msgs).toLocaleString()} msgs · ${topic.avg_hz}/${topic.nominal_hz}Hz · loss ${topic.loss_pct}%`;
    return `
      <tr>
        <td style="padding:4px 10px 4px 0;font-family:monospace;font-size:0.78rem;white-space:nowrap">${escapeHtml(topic.name || "")}</td>
        <td style="padding:4px 10px 4px 0;color:${color};font-weight:600">${topic.ok ? "ok" : "FAIL"}</td>
        <td style="padding:4px 0;color:var(--muted);font-size:0.8rem">${detail}</td>
      </tr>`;
  }).join("");
  const isImageOnly = report.scope === "image_streams";
  const headline = report.ok
    ? `<strong style="color:${okColor}">Complete</strong> — no frame loss above ${report.max_loss_pct}% on ${isImageOnly ? "recorded image streams" : "any topic"}`
    : `<strong style="color:${badColor}">Incomplete</strong> — frame loss on: ${escapeHtml((report.failed_topics || []).join(", "))}`
      + ` <span style="color:var(--muted)">(triage: USAGE.md §6.3)</span>`;
  integrityResultBody.innerHTML = `
    <div style="padding:12px 16px;border-radius:8px;background:var(--panel);border:1px solid var(--line)">
      <div style="margin-bottom:8px;font-size:0.95rem">${headline}</div>
      <div style="overflow-x:auto">
        <table style="border-collapse:collapse;width:100%">${rows}</table>
      </div>
    </div>`;
  integrityResultEl.hidden = false;
}

async function pollScoringStatus() {
  try {
    const response = await fetch(`/api/scoring/status?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      return;
    }
    const status = payload.status;
    if (status === "running") {
      scoringBusy = true;
      if (runScoringButton) {
        runScoringButton.disabled = true;
      }
      const topic = payload.topic ? ` — ${payload.topic}` : "";
      setScoringStatus(`Scoring${topic}`);
      scheduleScoringPoll(1500);
    } else if (status === "done") {
      scoringBusy = false;
      if (runScoringButton) {
        runScoringButton.disabled = false;
      }
      setScoringStatus(`Scored: ${payload.bag_name || ""}`);
      renderScoringResult(payload.result);
      void refreshRosbags();
    } else if (status === "error") {
      scoringBusy = false;
      if (runScoringButton) {
        runScoringButton.disabled = false;
      }
      setScoringStatus(`Error: ${payload.error || "Unknown error"}`);
    }
  } catch (_err) {
    // Silently ignore transient polling failures.
  }
}

function scheduleScoringPoll(delayMs) {
  if (scoringPollTimer !== null) {
    clearTimeout(scoringPollTimer);
  }
  scoringPollTimer = window.setTimeout(() => {
    scoringPollTimer = null;
    void pollScoringStatus();
  }, delayMs);
}

function setScoringStatus(message) {
  if (scoringStatusEyebrow) {
    scoringStatusEyebrow.hidden = !message;
  }
  if (scoringStatusEl) {
    scoringStatusEl.hidden = !message;
    scoringStatusEl.textContent = message;
  }
}

function hideScoringResult() {
  if (scoringResultEl) {
    scoringResultEl.hidden = true;
  }
}

function scoringColor(score) {
  return score >= 90 ? "#57d67c" : score >= 70 ? "#4aa8ff" : score >= 50 ? "#f0c040" : "#ff5a5a";
}

function renderScoringCameraCard(cam) {
  if (cam.error) {
    return `
      <div style="padding:12px 16px;border-radius:8px;background:var(--panel);border:1px solid var(--line)">
        <div style="font-family:monospace;font-size:0.78rem;color:var(--muted);margin-bottom:6px">${escapeHtml(cam.topic || "")}</div>
        <span style="color:#ff5a5a;font-size:0.85rem">Error: ${escapeHtml(cam.error)}</span>
      </div>`;
  }
  const color = scoringColor(cam.score || 0);
  const breakdownRows = [];
  if (cam.base_score !== undefined) {
    breakdownRows.push(`<tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">Base score</td><td>${escapeHtml(String(cam.base_score))}</td></tr>`);
    breakdownRows.push(`<tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">Transient blips</td><td>${escapeHtml(String(cam.transient_run_count || 0))} (-${escapeHtml(String(cam.transient_penalty || 0))})</td></tr>`);
    breakdownRows.push(`<tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">Sustained bad</td><td>${escapeHtml(String(cam.bad_run_count || 0))} run(s), ${escapeHtml(String(cam.bad_run_seconds || 0))}s (-${escapeHtml(String(cam.sustained_penalty || 0))})</td></tr>`);
    if (cam.episode_capped) {
      breakdownRows.push(`<tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">Episode cap</td><td style="color:#ff5a5a">bad run &gt; 1s, capped at 40</td></tr>`);
    }
    breakdownRows.push(`<tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">Usable</td><td>${escapeHtml((100 * (cam.usable_fraction !== undefined ? cam.usable_fraction : 1)).toFixed(1))}%</td></tr>`);
  }
  return `
    <div style="padding:12px 16px;border-radius:8px;background:var(--panel);border:1px solid var(--line)">
      <div style="font-family:monospace;font-size:0.78rem;color:var(--muted);margin-bottom:8px">${escapeHtml(cam.topic || "")}</div>
      <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:8px">
        <strong style="font-size:1.7rem;color:${escapeHtml(color)}">${escapeHtml(String(cam.score))} / 100</strong>
        <span style="font-size:0.95rem;color:var(--muted)">${escapeHtml(cam.quality || "")}</span>
      </div>
      <table style="border-collapse:collapse;width:100%;font-size:0.82rem">
        <tbody>
          ${breakdownRows.join("\n          ")}
          <tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">p50 trace</td><td>${escapeHtml((cam.p50_trace || 0).toExponential(3))}</td></tr>
          <tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">p90 trace</td><td>${escapeHtml((cam.p90_trace || 0).toExponential(3))}</td></tr>
          <tr><td class="page-copy" style="padding:0.15rem 0.5rem 0.15rem 0">Max trace</td><td>${escapeHtml((cam.max_trace || 0).toExponential(3))}</td></tr>
        </tbody>
      </table>
    </div>`;
}

function renderScoringResult(result) {
  if (!scoringResultEl || !scoringResultBody || !result) {
    return;
  }
  // multi-camera result: {cameras: [...]}
  if (result.cameras && Array.isArray(result.cameras)) {
    scoringResultBody.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
        ${result.cameras.map(renderScoringCameraCard).join("")}
      </div>`;
  } else {
    // legacy single-topic result
    const color = scoringColor(result.score || 0);
    scoringResultBody.innerHTML = `
      <div class="bag-row-main" style="margin-bottom:0.75rem">
        <strong style="font-size:2rem;color:${escapeHtml(color)}">${escapeHtml(String(result.score))} / 100</strong>
        <span style="font-size:1.1rem">${escapeHtml(result.quality || "")}</span>
      </div>
      <table style="border-collapse:collapse;width:100%;font-size:0.85rem">
        <tbody>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">Topic</td><td style="font-family:monospace;font-size:0.8rem">${escapeHtml(result.topic || "")}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">Mean cov trace</td><td>${escapeHtml((result.mean_trace || 0).toExponential(4))}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">Max cov trace</td><td>${escapeHtml((result.max_trace || 0).toExponential(4))}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">p90 cov trace</td><td>${escapeHtml((result.p90_trace || 0).toExponential(4))}</td></tr>
          <tr><td class="page-copy" style="padding:0.2rem 0.5rem 0.2rem 0">p99 cov trace</td><td>${escapeHtml((result.p99_trace || 0).toExponential(4))}</td></tr>
        </tbody>
      </table>`;
  }
  scoringResultEl.hidden = false;
}
