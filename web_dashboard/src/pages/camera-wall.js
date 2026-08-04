import {
  setCameraCapturePerformanceMode,
  startCameraDashboard,
} from "../camera/dashboard.js?v=20260804-split-kiosk";

const CAMERA_WINDOW_TITLE = "Insight Camera Wall [insight-kiosk-cameras]";
const CAMERA_FULLSCREEN_TITLE = "Insight Camera Wall [insight-kiosk-cameras-fullscreen]";
const KIOSK_STATE_POLL_MS = 1000;
let capturePerformanceMode = false;

document.title = CAMERA_WINDOW_TITLE;
window.addEventListener("insight:camera-maximized", (event) => {
  const maximized = Boolean(event.detail && event.detail.maximized);
  document.body.classList.toggle("camera-window-maximized", maximized);
  document.title = maximized ? CAMERA_FULLSCREEN_TITLE : CAMERA_WINDOW_TITLE;
});

void startCameraWall();

async function startCameraWall() {
  await refreshKioskState();
  startCameraDashboard({ cameraStaggerMs: 450 });
  window.setInterval(() => { void refreshKioskState(); }, KIOSK_STATE_POLL_MS);
}

async function refreshKioskState() {
  try {
    const response = await fetch(`/api/kiosk/state?ts=${Date.now()}`, {
      cache: "no-store",
    });
    if (!response.ok) return;
    const payload = await response.json();
    const nextMode = Boolean(payload.capture_performance);
    if (nextMode === capturePerformanceMode) return;
    capturePerformanceMode = nextMode;
    setCameraCapturePerformanceMode(capturePerformanceMode);
  } catch (_error) {
    // The camera stream remains usable while the lightweight state poll retries.
  }
}
