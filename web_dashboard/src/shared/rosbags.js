import { escapeHtml, formatDuration } from "./format.js";

const bagList = document.getElementById("bag-list");
const bagListStatus = document.getElementById("bag-list-status");
const refreshBagsButton = document.getElementById("refresh-bags-button");
const scoringBagMeta = document.getElementById("scoring-bag-meta");
const optimizationBagMeta = document.getElementById("optimization-bag-meta");
let knownRosbags = [];

async function refreshRosbags() {
  setBagListStatus("Loading bags...");
  try {
    const response = await fetch(`/api/rosbags?ts=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to load rosbags.");
    }
    knownRosbags = Array.isArray(payload.bags) ? payload.bags : [];
    renderBagList(knownRosbags);
    renderBagSelects(knownRosbags);
    setBagListStatus(`${knownRosbags.length} bags in ${payload.rosbag_root || "rosbags"}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setBagListStatus(message);
    renderBagList([]);
    renderBagSelects([]);
  }
}

function renderBagList(bags) {
  if (!bagList) {
    return;
  }
  if (!Array.isArray(bags) || bags.length === 0) {
    bagList.innerHTML = '<div class="empty-state">No local rosbags found yet.</div>';
    return;
  }
  bagList.innerHTML = bags.map((bag, index) => `
    <article class="bag-row">
      <div class="bag-row-identity">
        <span class="bag-index">${String(index + 1).padStart(2, "0")}</span>
        <div class="bag-row-main">
          <strong>${escapeHtml(bag.name || "unnamed bag")}</strong>
        </div>
      </div>
      <div class="bag-row-stats">
        <span><small>Duration</small><strong>${formatDuration(Number(bag.duration_s || 0))}</strong></span>
        <span><small>Size</small><strong>${escapeHtml(bag.size_label || "--")}</strong></span>
        <span><small>Messages</small><strong>${Number(bag.message_count || 0).toLocaleString()}</strong></span>
        <span><small>Topics</small><strong>${Number(bag.topic_count || 0)}</strong></span>
      </div>
      <div class="bag-badges">
        <span class="bag-badge ${bag.integrity === true ? "is-ok" : bag.integrity === false ? "is-bad" : ""}">${
          bag.integrity === true ? "complete" : bag.integrity === false ? "incomplete" : "unverified"
        }</span>
        <span class="bag-badge ${bag.scored ? "is-ok" : ""}">${bag.scored ? "scored" : "unscored"}</span>
        <span class="bag-badge ${bag.optimized ? "is-ok" : ""}">${bag.optimized ? "optimized" : "not optimized"}</span>
      </div>
      <div class="bag-row-actions">
        <button type="button" class="bag-delete-button" data-bag-name="${escapeHtml(bag.name || "")}">Delete</button>
      </div>
    </article>
  `).join("");
  bagList.querySelectorAll(".bag-delete-button").forEach((btn) => {
    btn.addEventListener("click", () => deleteBag(btn.dataset.bagName));
  });
}

function renderBagSelects(bags) {
  const selects = Array.from(document.querySelectorAll("[data-bag-select]"));
  if (selects.length === 0) {
    return;
  }
  selects.forEach((select) => {
    const previous = select.value;
    if (!Array.isArray(bags) || bags.length === 0) {
      select.innerHTML = '<option value="">No local rosbags found</option>';
      updateSelectedBagMeta(select);
      return;
    }
    select.innerHTML = bags.map((bag) => `<option value="${escapeHtml(bag.name || "")}">${escapeHtml(bag.name || "")}</option>`).join("");
    if (previous && bags.some((bag) => bag.name === previous)) {
      select.value = previous;
    }
    select.onchange = () => updateSelectedBagMeta(select);
    updateSelectedBagMeta(select);
  });
}

function updateSelectedBagMeta(select) {
  const bag = knownRosbags.find((item) => item.name === select.value);
  const meta = select.id === "optimization-bag-select" ? optimizationBagMeta : scoringBagMeta;
  if (!meta) {
    return;
  }
  if (!bag) {
    meta.textContent = "No rosbag selected.";
    return;
  }
  meta.textContent = `${formatDuration(Number(bag.duration_s || 0))} · ${bag.size_label || "--"} · ${Number(bag.message_count || 0).toLocaleString()} messages · ${bag.label || ""}`;
}

async function deleteBag(bagName) {
  if (!bagName) return;
  if (!confirm(`Delete bag "${bagName}"?\n\nThis will permanently remove the bag directory and cannot be undone.`)) return;
  try {
    const response = await fetch(`/api/rosbags/${encodeURIComponent(bagName)}`, { method: "DELETE" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      alert(`Failed to delete bag: ${payload.error || response.statusText}`);
      return;
    }
    void refreshRosbags();
  } catch (err) {
    alert(`Error deleting bag: ${err.message}`);
  }
}

function setBagListStatus(message) {
  if (bagListStatus) {
    bagListStatus.textContent = message;
  }
}

export function initializeRosbags() {
  if (refreshBagsButton) {
    refreshBagsButton.addEventListener("click", () => {
      void refreshRosbags();
    });
  }
  if (bagList || document.querySelector("[data-bag-select]")) {
    void refreshRosbags();
  }
}

export { refreshRosbags };
