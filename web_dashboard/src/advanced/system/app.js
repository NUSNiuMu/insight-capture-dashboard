import { escapeHtml } from "../shared/format.js";

const settingsPanel = document.getElementById("settings-panel");
const settingsStatus = document.getElementById("settings-status");
const settingsCameraList = document.getElementById("settings-camera-list");
const settingsRestartMessage = document.getElementById("settings-restart-message");
const settingsRestartButton = document.getElementById("settings-restart-button");
const insight3MaskForm = document.getElementById("insight3-mask-form");
const insight3MaskRatio = document.getElementById("insight3-mask-ratio");
const voiceVolumeSlider = document.getElementById("voice-volume-slider");
const voiceVolumeOutput = document.getElementById("voice-volume-output");
const voiceVolumeStatus = document.getElementById("voice-volume-status");
const voiceVolumeSampleButton = document.getElementById("voice-volume-sample-button");
let voiceVolumeTimer = null;
let pendingVoiceVolume = null;
let voiceVolumeSaving = false;
let voiceVolumeSamplePlaying = false;
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
if (insight3MaskForm) {
  insight3MaskForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void setInsight3GripperMaskRatio(insight3MaskRatio && insight3MaskRatio.value);
  });
}
if (voiceVolumeSlider) {
  voiceVolumeSlider.addEventListener("input", () => {
    const volume = Number(voiceVolumeSlider.value);
    renderVoiceVolumeValue(volume);
    pendingVoiceVolume = volume;
    if (voiceVolumeStatus) {
      voiceVolumeStatus.textContent = "Adjusting...";
    }
    window.clearTimeout(voiceVolumeTimer);
    voiceVolumeTimer = window.setTimeout(() => {
      void flushVoiceVolume();
    }, 120);
  });
  voiceVolumeSlider.addEventListener("change", () => {
    window.clearTimeout(voiceVolumeTimer);
    pendingVoiceVolume = Number(voiceVolumeSlider.value);
    void flushVoiceVolume();
  });
}
if (voiceVolumeSampleButton) {
  voiceVolumeSampleButton.addEventListener("click", () => {
    void playVoiceVolumeSample();
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

function renderSettings(payload) {
  renderVoiceAudio(payload && payload.voice_audio);
  if (!settingsCameraList) {
    return;
  }
  if (insight3MaskForm && insight3MaskRatio) {
    insight3MaskForm.hidden = false;
    insight3MaskRatio.value = String(payload.insight3_gripper_mask_height_ratio ?? 0.2);
  }
  const poses = Array.isArray(payload && payload.poses) ? payload.poses : [];
  settingsCameraList.innerHTML = poses.map((pose) => {
    const roleLabel = (ROLE_STYLE[pose.role] && ROLE_STYLE[pose.role].label) || pose.role;
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
        ${gripperRow}
        ${handOverlayRow}
      </article>
    `;
  }).join("");

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

function renderVoiceVolumeValue(volume) {
  if (voiceVolumeOutput) {
    voiceVolumeOutput.textContent = `${Math.round(volume)}%`;
  }
}

function renderVoiceAudio(audio) {
  if (!voiceVolumeSlider) {
    return;
  }
  const available = Boolean(audio && audio.available);
  voiceVolumeSlider.disabled = !available;
  if (voiceVolumeSampleButton) {
    voiceVolumeSampleButton.disabled = !available || voiceVolumeSamplePlaying;
  }
  if (!available) {
    voiceVolumeSlider.value = "0";
    if (voiceVolumeOutput) {
      voiceVolumeOutput.textContent = "--%";
    }
    if (voiceVolumeStatus) {
      voiceVolumeStatus.textContent = (audio && audio.error) || "Voice service unavailable.";
    }
    return;
  }
  const volume = Math.max(0, Math.min(100, Number(audio.volume_percent) || 0));
  voiceVolumeSlider.value = String(volume);
  renderVoiceVolumeValue(volume);
  if (voiceVolumeStatus) {
    voiceVolumeStatus.textContent = `${audio.backend === "pulse" ? "PulseAudio" : "ALSA"} · ${audio.playback_label || "active output"}`;
  }
}

async function flushVoiceVolume() {
  if (voiceVolumeSaving || pendingVoiceVolume === null) {
    return;
  }
  const volume = pendingVoiceVolume;
  pendingVoiceVolume = null;
  voiceVolumeSaving = true;
  try {
    const response = await fetch("/api/settings/voice-volume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume_percent: volume })
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to update speaker volume.");
    }
    renderVoiceAudio(payload.voice_audio);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (voiceVolumeStatus) {
      voiceVolumeStatus.textContent = `Failed to update volume: ${message}`;
    }
  } finally {
    voiceVolumeSaving = false;
    if (pendingVoiceVolume !== null) {
      void flushVoiceVolume();
    }
  }
}

async function playVoiceVolumeSample() {
  if (voiceVolumeSamplePlaying) {
    return;
  }
  voiceVolumeSamplePlaying = true;
  if (voiceVolumeSampleButton) {
    voiceVolumeSampleButton.disabled = true;
  }
  if (voiceVolumeStatus) {
    voiceVolumeStatus.textContent = "Playing sample...";
  }
  try {
    const response = await fetch("/api/settings/voice-sample", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to play sample.");
    }
    renderVoiceAudio(payload.voice_audio);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (voiceVolumeStatus) {
      voiceVolumeStatus.textContent = `Failed to play sample: ${message}`;
    }
  } finally {
    voiceVolumeSamplePlaying = false;
    if (voiceVolumeSampleButton) {
      voiceVolumeSampleButton.disabled = voiceVolumeSlider ? voiceVolumeSlider.disabled : false;
    }
  }
}

async function setInsight3GripperMaskRatio(rawValue) {
  const value = Number(rawValue);
  if (!Number.isFinite(value) || value < 0 || value >= 1) {
    setSettingsStatus("Insight3 gripper mask ratio must be between 0 and 1.");
    return;
  }
  setSettingsStatus("Updating Insight3 gripper mask...");
  try {
    const response = await fetch("/api/settings/insight3-gripper-mask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value })
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to update Insight3 gripper mask.");
    }
    renderSettings(payload);
    setSettingsStatus(`Insight3 gripper mask ratio updated to ${payload.insight3_gripper_mask_height_ratio}.`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    setSettingsStatus(`Failed to update Insight3 gripper mask: ${message}`);
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
