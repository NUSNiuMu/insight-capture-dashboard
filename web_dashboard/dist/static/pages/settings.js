import { escapeHtml } from "../shared/format.js";

const settingsPanel = document.getElementById("settings-panel");
const settingsStatus = document.getElementById("settings-status");
const settingsCameraList = document.getElementById("settings-camera-list");
const settingsRestartBanner = document.getElementById("settings-restart-banner");
const settingsRestartMessage = document.getElementById("settings-restart-message");
const settingsRestartButton = document.getElementById("settings-restart-button");
const ROLE_STYLE = {
  head: { label: "Head" },
  left_hand: { label: "Left Hand" },
  right_hand: { label: "Right Hand" },
};

if (settingsPanel) {
  void refreshSettings();
}
if (settingsRestartButton) {
  settingsRestartButton.addEventListener("click", () => {
    void restartBackend();
  });
}

async function refreshSettings() {
  if (!settingsPanel) {
    return null;
  }
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to load settings.");
    }
    renderSettings(payload);
    setSettingsStatus(`${payload.poses.length} camera(s) configured.`);
    return payload;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setSettingsStatus(`Failed to load settings: ${message}`);
    return null;
  }
}

function setSettingsStatus(message) {
  if (settingsStatus) {
    settingsStatus.textContent = message;
  }
}

// Avatar-model switching in the Settings UI; flip to true to re-lock the
// dropdown (the backend API stays available either way).
const AVATAR_MODEL_SWITCHING_LOCKED = false;

function renderSettings(payload) {
  if (!settingsCameraList) {
    return;
  }
  const stickRow = document.getElementById("stick-figure-row");
  const stickToggle = document.getElementById("stick-figure-toggle");
  if (stickRow && stickToggle) {
    stickRow.hidden = false;
    stickToggle.checked = Boolean(payload && payload.stick_figure_mode);
    if (!stickToggle.dataset.bound) {
      stickToggle.dataset.bound = "1";
      stickToggle.addEventListener("change", () => {
        void setStickFigureMode(stickToggle.checked);
      });
    }
  }
  const poses = Array.isArray(payload && payload.poses) ? payload.poses : [];
  const models = Array.isArray(payload && payload.available_models) ? payload.available_models : [];
  settingsCameraList.innerHTML = poses.map((pose) => {
    const roleLabel = (ROLE_STYLE[pose.role] && ROLE_STYLE[pose.role].label) || pose.role;
    const modelOptions = models.map((model) => {
      const selected = model.file === pose.avatar_model ? " selected" : "";
      return `<option value="${escapeHtml(model.file)}"${selected}>${escapeHtml(model.label)}</option>`;
    }).join("");
    const gripperRow = pose.gripper_tracking_available ? `
      <label class="settings-toggle-row">
        <input type="checkbox" class="settings-gripper-toggle" data-camera="${escapeHtml(pose.name)}" ${pose.gripper_tracking_enabled ? "checked" : ""}>
        <span>Gripper tracking</span>
      </label>
    ` : "";
    const handOverlayRow = pose.hand_overlay_available ? `
      <label class="settings-toggle-row">
        <input type="checkbox" class="settings-hand-overlay-toggle" data-camera="${escapeHtml(pose.name)}" ${pose.hand_overlay_enabled ? "checked" : ""}>
        <span>Hand landmark overlay</span>
      </label>
    ` : "";
    return `
      <article class="settings-camera-card">
        <div class="settings-camera-head">
          <h3>${escapeHtml(pose.name)}</h3>
          <span class="settings-role-pill">${escapeHtml(roleLabel)}</span>
        </div>
        <label class="settings-field">
          <span>Avatar model${AVATAR_MODEL_SWITCHING_LOCKED ? ' <span style="color:var(--muted);font-weight:400">(locked)</span>' : ""}</span>
          <select class="settings-model-select" data-camera="${escapeHtml(pose.name)}"${AVATAR_MODEL_SWITCHING_LOCKED ? " disabled" : ""}>${modelOptions}</select>
        </label>
        ${gripperRow}
        ${handOverlayRow}
      </article>
    `;
  }).join("");

  if (!AVATAR_MODEL_SWITCHING_LOCKED) {
    settingsCameraList.querySelectorAll(".settings-model-select").forEach((select) => {
      select.addEventListener("change", () => {
        void setPoseAvatarModel(select.dataset.camera, select.value);
      });
    });
  }
  settingsCameraList.querySelectorAll(".settings-gripper-toggle").forEach((toggle) => {
    toggle.addEventListener("change", () => {
      void setGripperTrackingEnabled(toggle.dataset.camera, toggle.checked);
    });
  });
  settingsCameraList.querySelectorAll(".settings-hand-overlay-toggle").forEach((toggle) => {
    toggle.addEventListener("change", () => {
      void setHandOverlayEnabled(toggle.dataset.camera, toggle.checked);
    });
  });
}

async function setStickFigureMode(enabled) {
  setSettingsStatus("Updating stick-figure mode...");
  try {
    const response = await fetch("/api/settings/stick-figure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled })
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to update stick-figure mode.");
    }
    renderSettings(payload);
    setSettingsStatus(`Stick-figure mode ${enabled ? "enabled" : "disabled"}.`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setSettingsStatus(`Failed to update stick-figure mode: ${message}`);
  }
}

async function setHandOverlayEnabled(name, enabled) {
  setSettingsStatus(`Updating ${name}...`);
  try {
    const response = await fetch("/api/settings/hand-overlay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled })
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to update hand overlay.");
    }
    renderSettings(payload);
    setSettingsStatus(`${name}: hand overlay ${enabled ? "enabled" : "disabled"}.`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setSettingsStatus(`Failed to update ${name}: ${message}`);
  }
}

async function setPoseAvatarModel(name, model) {
  setSettingsStatus(`Updating ${name}...`);
  try {
    const response = await fetch("/api/settings/avatar-model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, model })
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to update avatar model.");
    }
    renderSettings(payload);
    setSettingsStatus(`${name}: avatar model updated.`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setSettingsStatus(`Failed to update ${name}: ${message}`);
  }
}

async function setGripperTrackingEnabled(name, enabled) {
  setSettingsStatus(`Updating ${name}...`);
  try {
    const response = await fetch("/api/settings/gripper-tracking", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled })
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to update gripper tracking.");
    }
    renderSettings(payload);
    setSettingsStatus(`${name}: gripper tracking ${enabled ? "enabled" : "disabled"}.`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setSettingsStatus(`Failed to update ${name}: ${message}`);
  }
}

function showRestartBanner(message) {
  if (!settingsRestartBanner) {
    return;
  }
  if (settingsRestartMessage) {
    settingsRestartMessage.textContent = message || "Saved. Restart the backend to apply.";
  }
  settingsRestartBanner.hidden = false;
}

async function restartBackend() {
  if (!settingsRestartButton) {
    return;
  }
  settingsRestartButton.disabled = true;
  if (settingsRestartMessage) {
    settingsRestartMessage.textContent = "Restarting backend...";
  }
  try {
    await fetch("/api/settings/restart-backend", { method: "POST" });
  } catch (_error) {
    // Expected: the process exits before the response finishes.
  }
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    try {
      const response = await fetch("/healthz", { cache: "no-store" });
      if (response.ok) {
        window.location.reload();
        return;
      }
    } catch (_error) {
      // Backend still down; keep polling.
    }
  }
  if (settingsRestartMessage) {
    settingsRestartMessage.textContent = "Backend did not come back within 30s -- check it manually.";
  }
  settingsRestartButton.disabled = false;
}
