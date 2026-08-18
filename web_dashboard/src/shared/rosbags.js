import { escapeHtml, formatDuration } from "./format.js";

const bagList = document.getElementById("bag-list");
const bagListStatus = document.getElementById("bag-list-status");
const refreshBagsButton = document.getElementById("refresh-bags-button");
const scoringBagMeta = document.getElementById("scoring-bag-meta");
const optimizationBagMeta = document.getElementById("optimization-bag-meta");
const handposeBagMeta = document.getElementById("handpose-bag-meta");
let knownRosbags = [];
let rosbagScope = "all";
let initialBagReference = "";

async function refreshRosbags() {
  setBagListStatus("Loading bags...");
  try {
    const query = new URLSearchParams({ scope: rosbagScope, ts: String(Date.now()) });
    const response = await fetch(`/api/rosbags?${query}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to load rosbags.");
    }
    knownRosbags = Array.isArray(payload.bags) ? payload.bags : [];
    renderBagList(knownRosbags);
    renderBagSelects(knownRosbags);
    const scopeLabel = rosbagScope === "current"
      ? `current recording directory · ${payload.rosbag_root || "rosbags"}`
      : `${Array.isArray(payload.library_roots) ? payload.library_roots.length : 1} storage root(s)`;
    setBagListStatus(`${knownRosbags.length} bags · ${scopeLabel}`);
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
          <small>${escapeHtml(bagLocationLabel(bag))}</small>
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
        <span class="bag-badge ${bag.review_state === "ready" ? "is-ok" : bag.review_state === "invalid" ? "is-bad" : ""}">${escapeHtml(reviewStatusLabel(bag))}</span>
      </div>
      <div class="bag-row-actions">
        <button type="button" class="bag-delete-button" data-bag-ref="${escapeHtml(bagReference(bag))}">Delete</button>
      </div>
    </article>
  `).join("");
  bagList.querySelectorAll(".bag-delete-button").forEach((btn) => {
    btn.addEventListener("click", () => deleteBag(btn.dataset.bagRef));
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
    select.innerHTML = bags.map((bag) => `<option value="${escapeHtml(bagReference(bag))}">${escapeHtml(bag.name || "")} · ${escapeHtml(bagLocationLabel(bag))} · ${bag.review_state === "ready" ? "ready" : "pending"}</option>`).join("");
    if (previous && bags.some((bag) => bagReference(bag) === previous)) {
      select.value = previous;
    } else if (initialBagReference && bags.some((bag) => bagReference(bag) === initialBagReference)) {
      select.value = initialBagReference;
    }
    select.onchange = () => updateSelectedBagMeta(select);
    updateSelectedBagMeta(select);
  });
}

function reviewStatusLabel(bag) {
  if (bag.review_state === "ready") return `review ${bag.review_quality || "ready"}`;
  if (bag.review_state === "building") return "review building";
  if (bag.review_state === "invalid") return "review invalid";
  return "review pending";
}

function updateSelectedBagMeta(select) {
  const bag = knownRosbags.find((item) => bagReference(item) === select.value);
  const metaBySelect = {
    "scoring-bag-select": scoringBagMeta,
    "optimization-bag-select": optimizationBagMeta,
    "handpose-bag-select": handposeBagMeta,
  };
  const meta = metaBySelect[select.id];
  if (!meta) {
    return;
  }
  if (!bag) {
    meta.textContent = "No rosbag selected.";
    return;
  }
  meta.textContent = `${formatDuration(Number(bag.duration_s || 0))} · ${bag.size_label || "--"} · ${Number(bag.message_count || 0).toLocaleString()} messages · ${bag.label || ""}`;
}

function bagReference(bag) {
  return String((bag && (bag.id || bag.name)) || "");
}

function bagLocationLabel(bag) {
  const relative = String(bag?.relative_path || "");
  if (relative && relative !== bag?.name) return relative;
  return String(bag?.root || relative || "recording library");
}

async function deleteBag(bagRef) {
  if (!bagRef) return;
  const bag = knownRosbags.find((item) => bagReference(item) === bagRef);
  const label = bag?.name || bagRef;
  if (!confirm(`Delete bag "${label}"?\n\nThis will permanently remove the bag directory and cannot be undone.`)) return;
  try {
    const response = await fetch(`/api/rosbags/${encodeURIComponent(bagRef)}`, { method: "DELETE" });
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

export function initializeRosbags(options = {}) {
  rosbagScope = options.scope === "current" ? "current" : "all";
  initialBagReference = String(options.initialBag || "");
  if (refreshBagsButton) {
    refreshBagsButton.addEventListener("click", () => {
      void refreshRosbags();
    });
  }
  document.querySelectorAll("[data-bag-scope]").forEach((button) => {
    const scope = button.dataset.bagScope === "current" ? "current" : "all";
    button.classList.toggle("is-active", scope === rosbagScope);
    button.addEventListener("click", () => {
      rosbagScope = scope;
      document.querySelectorAll("[data-bag-scope]").forEach((item) => {
        item.classList.toggle("is-active", item.dataset.bagScope === rosbagScope);
      });
      void refreshRosbags();
    });
  });
  if (bagList || document.querySelector("[data-bag-select]")) {
    void refreshRosbags();
  }
}

export { refreshRosbags };
