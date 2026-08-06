const DATASET_ROOT = "outputs/stopwatch_sync_test_20260806_165749_first10";
const MANIFEST_PATH = `${DATASET_ROOT}/manifest.json`;
const CAMERA_ORDER = ["insight3_a", "insight3_b", "insight9_a"];
const CAMERA_LABELS = {
  insight3_a: "Insight3 A",
  insight3_b: "Insight3 B",
  insight9_a: "Insight9 A",
};

const selector = document.getElementById("frame-selector");
const comparison = document.getElementById("camera-comparison");
const status = document.getElementById("review-status");
const tableBody = document.getElementById("stopwatch-table-body");
const summarySpan = document.getElementById("summary-span");
const summarySpanDetail = document.getElementById("summary-span-detail");
const previousButton = document.getElementById("previous-frame");
const nextButton = document.getElementById("next-frame");
const dialog = document.getElementById("image-dialog");
const dialogImage = document.getElementById("dialog-image");
const dialogCaption = document.getElementById("dialog-caption");

let frames = new Map();
let selectedFrame = 1;
let timestampOrigin = 0n;

function assetUrl(relativePath) {
  return `/asset?path=${encodeURIComponent(`${DATASET_ROOT}/${relativePath}`)}`;
}

function formatDelta(deltaNs) {
  const milliseconds = Number(deltaNs) / 1e6;
  return `${milliseconds >= 0 ? "+" : ""}${milliseconds.toFixed(3)} ms`;
}

function formatRelative(timestampNs) {
  return `T+${(Number(BigInt(timestampNs) - timestampOrigin) / 1e6).toFixed(3)} ms`;
}

function groupSpan(group) {
  const stamps = group.map((entry) => BigInt(entry.header_ns));
  return { earliest: stamps.reduce((a, b) => a < b ? a : b), span: stamps.reduce((a, b) => a > b ? a : b) - stamps.reduce((a, b) => a < b ? a : b) };
}

function renderSelector() {
  selector.replaceChildren();
  for (const frameIndex of frames.keys()) {
    const button = document.createElement("button");
    button.type = "button";
    button.role = "tab";
    button.textContent = String(frameIndex).padStart(2, "0");
    button.className = frameIndex === selectedFrame ? "is-active" : "";
    button.setAttribute("aria-selected", String(frameIndex === selectedFrame));
    button.addEventListener("click", () => selectFrame(frameIndex));
    selector.append(button);
  }
}

function cameraCard(entry, earliest) {
  const article = document.createElement("article");
  article.className = "stopwatch-camera-card";
  const delta = BigInt(entry.header_ns) - earliest;
  const cropUrl = assetUrl(entry.stopwatch_crop);
  const boxedUrl = assetUrl(entry.boxed_image);
  article.innerHTML = `
    <header><div><span>Camera</span><h2>${CAMERA_LABELS[entry.camera]}</h2></div><b>${formatDelta(delta)}</b></header>
    <button type="button" class="stopwatch-crop-button" data-image="${cropUrl}" data-caption="${CAMERA_LABELS[entry.camera]} · 第 ${entry.frame_index} 帧 · 秒表 ${entry.stopwatch_reading}">
      <img src="${cropUrl}" alt="${CAMERA_LABELS[entry.camera]} 第 ${entry.frame_index} 帧秒表裁剪">
      <span><small>秒表读数</small><strong>${entry.stopwatch_reading}</strong></span>
    </button>
    <button type="button" class="stopwatch-boxed-button" data-image="${boxedUrl}" data-caption="${CAMERA_LABELS[entry.camera]} · 第 ${entry.frame_index} 帧 · ${entry.header_utc}">
      <img src="${boxedUrl}" alt="${CAMERA_LABELS[entry.camera]} 第 ${entry.frame_index} 帧红框原图">
      <i>点击放大原图</i>
    </button>
    <dl>
      <div><dt>RELATIVE MS</dt><dd title="Header ns: ${entry.header_ns}">${formatRelative(entry.header_ns)}</dd></div>
      <div><dt>UTC</dt><dd>${entry.header_utc}</dd></div>
      <div><dt>TOPIC</dt><dd title="${entry.topic}">${entry.topic}</dd></div>
    </dl>`;
  return article;
}

function renderComparison() {
  const group = frames.get(selectedFrame);
  if (!group) return;
  const { earliest, span } = groupSpan(group);
  comparison.replaceChildren(...group.map((entry) => cameraCard(entry, earliest)));
  comparison.hidden = false;
  status.hidden = true;
  summarySpan.textContent = `${(Number(span) / 1e6).toFixed(3)} ms`;
  const first = group.reduce((a, b) => BigInt(a.header_ns) < BigInt(b.header_ns) ? a : b);
  const last = group.reduce((a, b) => BigInt(a.header_ns) > BigInt(b.header_ns) ? a : b);
  summarySpanDetail.textContent = `${CAMERA_LABELS[first.camera]} 最早 · ${CAMERA_LABELS[last.camera]} 最晚`;
  previousButton.disabled = selectedFrame === Math.min(...frames.keys());
  nextButton.disabled = selectedFrame === Math.max(...frames.keys());
  renderSelector();
  document.querySelectorAll("[data-image]").forEach((button) => button.addEventListener("click", () => openImage(button.dataset.image, button.dataset.caption)));
}

function renderTable() {
  tableBody.replaceChildren();
  for (const [frameIndex, group] of frames) {
    const { span } = groupSpan(group);
    const byCamera = Object.fromEntries(group.map((entry) => [entry.camera, entry]));
    const row = document.createElement("tr");
    row.tabIndex = 0;
    row.innerHTML = `<td><strong>${String(frameIndex).padStart(2, "0")}</strong></td>${CAMERA_ORDER.map((camera) => `<td><strong>${byCamera[camera].stopwatch_reading}</strong><small class="relative-time relative-time-${camera}" title="Header ns: ${byCamera[camera].header_ns}"><i></i>${formatRelative(byCamera[camera].header_ns)}</small></td>`).join("")}<td><b>${(Number(span) / 1e6).toFixed(3)} ms</b></td>`;
    const openRow = () => { selectFrame(frameIndex); window.scrollTo({ top: 250, behavior: "smooth" }); };
    row.addEventListener("click", openRow);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") openRow(); });
    tableBody.append(row);
  }
}

function selectFrame(frameIndex) {
  selectedFrame = frameIndex;
  renderComparison();
}

function openImage(src, caption) {
  dialogImage.src = src;
  dialogCaption.textContent = caption;
  dialog.showModal();
}

previousButton.addEventListener("click", () => selectFrame(selectedFrame - 1));
nextButton.addEventListener("click", () => selectFrame(selectedFrame + 1));
document.getElementById("close-dialog").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
document.addEventListener("keydown", (event) => {
  if (dialog.open) return;
  if (event.key === "ArrowLeft" && !previousButton.disabled) selectFrame(selectedFrame - 1);
  if (event.key === "ArrowRight" && !nextButton.disabled) selectFrame(selectedFrame + 1);
});

try {
  const response = await fetch(`/asset?path=${encodeURIComponent(MANIFEST_PATH)}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  // Preserve nanosecond integers exactly; JSON.parse would round values above
  // Number.MAX_SAFE_INTEGER before the comparison view can use them.
  const manifestText = await response.text();
  const manifest = JSON.parse(manifestText.replace(/"(header_ns|receipt_ns)"\s*:\s*(\d+)/g, '"$1":"$2"'));
  timestampOrigin = manifest.reduce((earliest, entry) => {
    const stamp = BigInt(entry.header_ns);
    return earliest === 0n || stamp < earliest ? stamp : earliest;
  }, 0n);
  document.getElementById("timeline-origin").title = `T0 header ns: ${timestampOrigin}`;
  for (const entry of manifest) {
    if (!frames.has(entry.frame_index)) frames.set(entry.frame_index, []);
    frames.get(entry.frame_index).push(entry);
  }
  frames = new Map([...frames].sort(([a], [b]) => a - b).map(([index, group]) => [index, CAMERA_ORDER.map((camera) => group.find((entry) => entry.camera === camera)).filter(Boolean)]));
  renderSelector();
  renderComparison();
  renderTable();
} catch (error) {
  status.textContent = `秒表数据载入失败：${error.message}`;
  status.classList.add("is-error");
}
