import { Trajectory, poseFromState } from "./trajectory.js";
import { DevicePanel } from "./devices.js";
import { LerobotPlayer } from "./lerobot.js";
const $ = (id) => document.getElementById(id),
  sides = ["left", "right"];
const names = {
  head: "Insight9 观察相机",
  rgb_left_up: "左手上侧 RGB",
  rgb_left_down: "左手下侧 RGB",
  rgb_right_up: "右手上侧 RGB",
  rgb_right_down: "右手下侧 RGB",
  left: "左手",
  right: "右手",
  both: "双手",
};
const reasonText = {
  marker_not_detected: "未检测到 cube",
  not_enough_markers_visible: "可见标记数量不足",
  pnp_solve_failed: "位姿求解失败",
  low_inlier_ratio: "内点比例过低",
  insufficient_face_support: "有效标记面不足，几何不稳定",
  degenerate_geometry: "标记共面/退化，无法求解",
  depth_out_of_range: "推算深度超出合理范围",
  reprojection_too_high: "重投影误差过大",
  pose_jump: "位姿跳变，当前帧无效",
  head_vio_reset: "VIO 重置，请重新录制",
  missing_head_pose_or_calibration: "缺少同步 VIO 或标定",
  image_decode_failed: "画面解码失败",
};
let mode = "capture",
  status = null,
  records = [],
  selectedName = "",
  review = null,
  parts = [],
  dirty = false,
  // The segments are a generated draft (pause boundaries or the model's) that
  // no one has edited yet: not saved, but nothing is lost by leaving, since the
  // draft is on disk and loads again. Only edits make the segments `dirty`.
  generated = false,
  // A 模型逐段标注 request is being queued / a 中文转英文 request is running.
  labelBusy = false,
  translateBusy = false,
  // Model jobs of every recording (GET /api/jobs), for the queue panel.
  jobList = [],
  editing = -1,
  // Segments picked for merging on the timeline, held by object so they
  // survive re-sorting; `anchor` is where a Shift-click range starts.
  selection = new Set(),
  anchor = null,
  playing = false,
  currentTime = 0,
  frameBusy = false,
  frameVersion = 0,
  liveRows = [],
  trailMode = true,
  cursor = 0,
  epoch = -1,
  liveStreams = [],
  renderedStreams = "",
  lastPoseTime = 0,
  actionBusy = false,
  exportBusy = false,
  reviewBusy = false,
  editorDirty = false,
  lastRenderedFrame = "",
  exportSelection = new Set(),
  datasets = [],
  datasetDetailName = "",
  poseInfo = null,
  poseTimer = null,
  poseFilterTimer = null,
  // Shared task/subtask vocabulary; segments keep its ids next to free text.
  actionTable = { version: "", tasks: [] },
  // Pre-annotation service status and the open session's draft / job.
  service = null,
  draftInfo = null,
  draftTimer = null;
const CJK = /[\u3400-\u9fff]/;
const scene = new Trajectory($("scene"), $("sceneEmpty"));
const lerobot = new LerobotPlayer(notice);
const devicePanel = new DevicePanel(api);
const el = (tag, text, cls) => {
  const x = document.createElement(tag);
  if (text !== undefined) x.textContent = text;
  if (cls) x.className = cls;
  return x;
};
// Messages float over the page (see #message in the CSS): showing, growing or
// hiding one never shifts the layout under the timeline or the video.
function notice(text, error = false) {
  $("message").hidden = !text;
  $("message").textContent = text;
  $("message").className = error ? "error" : "";
  if (!text) return;
  const close = el("button", "×", "quiet toast-close");
  close.type = "button";
  close.setAttribute("aria-label", "关闭提示");
  close.onclick = () => notice("");
  $("message").append(close);
}
async function api(path, method = "GET", body) {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw Error(
      r.ok
        ? "服务器返回了无法读取的数据"
        : `请求失败（${r.status}），请检查服务连接`,
    );
  }
  if (!r.ok) throw Error(data.error || `请求失败（${r.status}）`);
  return data;
}
function base() {
  if (!selectedName) throw Error("请先选择一条已停止的录制");
  return "/api/sessions/" + encodeURIComponent(selectedName);
}
function seconds(t, precision = 2) {
  const n = Math.max(0, Number(t) || 0);
  return `${String(Math.floor(n / 60)).padStart(2, "0")}:${(n % 60).toFixed(precision).padStart(precision + 3, "0")}`;
}
function title(name) {
  return names[name] || name;
}
function setDirty(value) {
  dirty = value;
  generated = false;
  $("dirty").hidden = !value;
  clearTimeout(wipTimer);
  if (value && review && selectedName) wipTimer = setTimeout(saveWip, 2000);
}
// Segments replaced by a generated draft: an untouched replacement of nothing
// (no segments, or an earlier untouched draft) is not an edit.
function installGenerated(next) {
  const replacing = dirty || (parts.length > 0 && !generated);
  parts = next;
  setDirty(replacing);
  generated = !replacing;
  return replacing;
}
// Unsaved edits are stashed on the server (segments_wip.json) two seconds after
// the last change, so a reload or a crashed browser does not lose them;
// 保存全部标注 still writes segments.json and clears the stash.
let wipTimer = null;
async function saveWip() {
  if (!dirty || !review || !selectedName) return;
  const name = selectedName;
  try {
    await api(base() + "/wip", "POST", { segments: parts });
  } catch (e) {
    if (name === selectedName) notice("未保存的修改暂存失败：" + e.message, true);
  }
}
async function offerWip(name) {
  let wip;
  try {
    wip = await api(base() + "/wip");
  } catch {
    return;
  }
  if (!wip || name !== selectedName || dirty) return;
  const when = new Date(wip.saved_unix_ns / 1e6).toLocaleString();
  notice(`这条录制有 ${when} 暂存的未保存修改（${wip.segments.length} 段）。`);
  const restore = el("button", "恢复", "quiet"),
    drop = el("button", "丢弃", "quiet");
  restore.onclick = () => {
    if (name !== selectedName) return;
    const previous = parts.map((p) => ({ ...p }));
    parts = wip.segments;
    selection = new Set();
    resetEditor();
    renderParts();
    setDirty(true);
    notice("已恢复暂存的修改，确认后点「保存全部标注」");
    offerUndo("撤销恢复", previous);
  };
  drop.onclick = async () => {
    await api(base() + "/wip", "DELETE").catch(() => {});
    notice("已丢弃暂存的修改");
  };
  $("message").append(restore, drop);
}
function bind(id, fn) {
  $(id).addEventListener("click", async () => {
    const b = $(id);
    b.disabled = true;
    try {
      await fn();
    } catch (e) {
      notice(e.message, true);
    } finally {
      b.disabled = false;
      updateControls();
    }
  });
}
function updateControls() {
  updateModelButtons();
  const active = !!status?.recording;
  $("start").hidden = active;
  $("stop").hidden = !active;
  $("start").disabled =
    actionBusy || exportBusy || !status || status?.reconfiguring;
  $("stop").disabled = actionBusy;
  $("play").disabled = !review;
  for (const id of ["prevFrame", "nextFrame", "qc", "save", "add", "autoSegment", "splitPart", "moveBoundary"])
    $(id).disabled = !review || reviewBusy;
  $("sessions").disabled = reviewBusy || exportBusy;
  for (const id of ["from", "to", "task", "taskId", "subtask", "hands"])
    $(id).disabled = reviewBusy;
  $("subtaskId").disabled = reviewBusy || !tableTask($("taskId").value);
  for (const id of ["poseSource", "poseFilter", "poseRecompute"])
    $(id).disabled = !review || reviewBusy || !!poseInfo?.job?.running;
  updateMerge();
  renderDraft();
  updateSessionNav();
  updateBatchExport();
}
const FULLSCREEN_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>';
function videoCards(streams) {
  const key = streams.join("|");
  if (renderedStreams === key) return;
  renderedStreams = key;
  for (const img of $("videos").querySelectorAll("img[data-object]"))
    URL.revokeObjectURL(img.dataset.object);
  $("videos").replaceChildren();
  for (const name of streams) {
    const card = el("div", undefined, "video-card");
    card.dataset.stream = name;
    const wrap = el("div", undefined, "image-wrap"),
      image = el("img");
    image.width = 640;
    image.height = 360;
    image.alt = title(name) + "画面";
    image.hidden = true;
    const empty = el("div", undefined, "video-empty");
    empty.innerHTML =
      '<svg viewBox="0 0 32 32" aria-hidden="true"><rect x="3" y="7" width="19" height="18" rx="3"/><path d="m22 13 7-4v14l-7-4"/><circle cx="12.5" cy="16" r="4"/></svg>';
    empty.append(el("span", "等待相机画面"));
    wrap.append(image, empty);
    const label = el("div", undefined, "video-label");
    label.append(el("strong", title(name)), el("span", "未连接", "video-rate"));
    const expand = el("button", undefined, "quiet");
    expand.innerHTML = FULLSCREEN_ICON;
    expand.setAttribute("aria-label", "全屏查看" + title(name));
    expand.dataset.fullscreen = "";
    expand.onclick = () => toggleFullscreen(card);
    label.append(expand);
    card.append(wrap, label);
    $("videos").append(card);
  }
  $("cameraCount").textContent = `${streams.length} 路视频`;
}
async function loadImage(card, url, valid = true, signal) {
  const img = card.querySelector("img"),
    empty = card.querySelector(".video-empty");
  if (!valid) {
    img.hidden = true;
    empty.hidden = false;
    empty.querySelector("span").textContent =
      mode === "capture" ? "等待相机画面" : "此时刻图像缺失";
    return;
  }
  try {
    const response = await fetch(url, { signal });
    if (!response.ok) throw Error();
    const object = URL.createObjectURL(await response.blob());
    const image = new Image();
    image.src = object;
    await image.decode();
    return { card, object };
  } catch {
    // A deliberately cancelled request is not a missing frame: return
    // nothing so the card keeps showing its current picture.
    if (signal?.aborted) return null;
    return { card, object: null };
  }
}
function applyImages(results, version) {
  for (const value of results) {
    if (!value) continue;
    const { card, object } = value;
    if (version !== frameVersion) {
      if (object) URL.revokeObjectURL(object);
      continue;
    }
    const img = card.querySelector("img"),
      empty = card.querySelector(".video-empty");
    if (img.dataset.object) URL.revokeObjectURL(img.dataset.object);
    if (object) {
      img.src = object;
      img.dataset.object = object;
      img.hidden = false;
      empty.hidden = true;
    } else {
      img.hidden = true;
      empty.hidden = false;
      empty.querySelector("span").textContent = "此时刻图像缺失";
    }
  }
}
function armCards(arms = {}, grippers = {}, rates = {}) {
  $("armCards").replaceChildren();
  for (const side of sides) {
    const p = arms[side],
      g = grippers[side],
      rate = rates["gripper/" + side],
      card = el("section", undefined, "arm-card " + side),
      heading = el("div", undefined, "arm-title"),
      status = el("div", undefined, "arm-status-group");
    status.append(
      el(
        "span",
        p?.valid ? "定位有效" : reasonText[p?.reason] || "等待有效定位",
        "status" + (p?.valid ? " valid" : ""),
      ),
      el("span", rate ? `${rate} Hz` : "—", "rate"),
    );
    heading.append(el("strong", title(side) + " TCP"), status);
    const values = el("div", undefined, "arm-values"),
      coords = el("div", undefined, "coordinates");
    ["X", "Y", "Z"].forEach((axis, i) => {
      const c = el("div");
      c.append(
        el("span", axis + " / m"),
        el("strong", p?.valid ? p.position[i].toFixed(3) : "—"),
      );
      coords.append(c);
    });
    const grip = el("div", undefined, "gripper-value");
    grip.append(
      el("span", "开合度 / mm"),
      el("strong", g?.valid ? (g.width_m * 1000).toFixed(1) : "—"),
    );
    const meter = el("div", undefined, "gripper-meter"),
      fill = el("i");
    fill.style.width = g?.valid ? `${Math.min(100, g.width_m * 1000)}%` : "0";
    meter.append(fill);
    grip.append(meter);
    values.append(coords, grip);
    const detail =
      p?.valid && p.marker_ids
        ? `标记 ${p.marker_ids.join(", ")} · ${p.single_face ? "单面定位" : "多面定位"} · 重投影 ${p.reprojection_px.toFixed(2)} px`
        : mode === "capture"
          ? "等待 cube 观测及串口开合度"
          : "与当前回放时刻同步";
    card.append(heading, values, el("p", detail, "arm-detail"));
    $("armCards").append(card);
  }
}
function liveArms(s) {
  return Object.fromEntries(
    sides.map((side) => {
      const p = s.arms[s.arm_bindings[side]];
      return [
        side,
        p?.matrix
          ? {
              ...p,
              position: p.matrix.slice(0, 3).map((r) => r[3]),
              rotation: p.matrix.slice(0, 3).map((r) => r.slice(0, 3)),
            }
          : p,
      ];
    }),
  );
}
function readiness(s) {
  const age = (k) => s.sources_age_s[k] ?? Infinity;
  const items = [
    [
      "Insight9 图像",
      age("image/head") < 2,
      age("image/head") < 2
        ? `${s.source_rates_hz["image/head"] || 0} Hz`
        : "等待接入",
    ],
    s.vio_enabled
      ? [
          "VIO",
          age("vio/head") < 2,
          age("vio/head") < 2
            ? `${s.source_rates_hz["vio/head"] || 0} Hz`
            : "等待接入",
        ]
      : ["VIO", true, "已禁用（固定相机模式）"],
    [
      "内参 / 外参",
      s.calibrated,
      s.calibrated
        ? "就绪"
        : !s.calibration.intrinsic
          ? "缺少相机内参"
          : "缺少 IMU→RGB 外参",
    ],
    ...sides.map((side) => [
      title(side) + "编码器",
      !!s.grippers[side]?.valid,
      s.grippers[side]?.valid ? "就绪" : "未就绪",
    ]),
  ];
  $("readiness").replaceChildren(
    ...items.map(([label, ok, value]) => {
      const d = el("div", undefined, "ready-item" + (ok ? " ok" : ""));
      d.append(el("i"), el("strong", label), el("span", value));
      return d;
    }),
  );
}
function livePeriodMs() {
  return Math.max(8, Math.round(1000 / (status?.fps || 20)));
}
const sampleRateEdit = { until: 0, pending: false, committedAt: 0 };
async function pollStatus() {
  try {
    const requested = performance.now();
    const s = await api("/api/status");
    status = s;
    liveStreams = s.streams;
    if (mode === "capture") scene.setCameraView(s.camera_view);
    $("connection").textContent = "服务已连接";
    $("connectionDot").className = "online";
    $("elapsed").textContent = seconds(s.elapsed_s, 1);
    $("recordingState").textContent = s.recording ? "正在录制" : "未录制";
    $("recordingState").className = "badge" + (s.recording ? " recording" : "");
    // Never let the poll overwrite the field mid-edit, while a commit is in
    // flight, or with a response that was requested before that commit.
    const editingRate =
      document.activeElement === $("sampleRate") ||
      sampleRateEdit.pending ||
      performance.now() < sampleRateEdit.until ||
      requested < sampleRateEdit.committedAt;
    if (!editingRate) $("sampleRate").value = s.fps;
    $("sampleRate").disabled = !!s.recording;
    $("recordName").disabled = !!s.recording;
    if (document.activeElement !== $("vioEnabled")) $("vioEnabled").checked = s.vio_enabled;
    $("vioEnabled").disabled = !!s.recording;
    readiness(s);
    devicePanel.update(s);
    $("footerState").textContent = s.recording
      ? `正在保存 ${s.recording}`
      : s.calibrated
        ? "采集服务运行中"
        : "采集服务运行中，等待设备数据";
    if (mode === "capture") {
      videoCards(s.streams);
      armCards(liveArms(s), s.grippers, s.source_rates_hz);
      for (const card of $("videos").children) {
        const name = card.dataset.stream;
        card.querySelector(".video-rate").textContent =
          (s.sources_age_s["image/" + name] ?? Infinity) < 2
            ? `${s.source_rates_hz["image/" + name] || 0} Hz`
            : "未连接";
      }
      scene.cursor({ arms: liveArms(s) });
    }
    if (s.error) notice(s.error, true);
    updateControls();
  } catch (e) {
    status = null;
    devicePanel.update(null);
    $("connection").textContent = "服务连接中断";
    $("connectionDot").className = "";
    $("footerState").textContent = "连接中断，请检查服务是否运行";
    if (mode === "capture") {
      armCards();
      scene.cursor(null);
      for (const card of $("videos").children) loadImage(card, "", false);
    }
    updateControls();
  }
  setTimeout(pollStatus, livePeriodMs());
}
function recentRows(rows, seconds) {
  if (!trailMode || !rows.length) return rows;
  const now = rows[rows.length - 1].t;
  const cutoff = rows.findIndex((r) => now - r.t <= seconds);
  return cutoff <= 0 ? rows : rows.slice(cutoff);
}
async function pollLive() {
  if (mode === "capture" && status && !document.hidden) {
    try {
      if (epoch !== status.history_epoch) {
        epoch = status.history_epoch;
        cursor = 0;
        liveRows = [];
        lastPoseTime = 0;
        scene.setData([]);
      }
      const data = await api("/api/live/trajectory?after=" + cursor);
      if (data.epoch !== epoch) {
        epoch = data.epoch;
        cursor = 0;
        liveRows = [];
      } else if (data.rows.length && mode === "capture") {
        for (const r of data.rows) {
          cursor = Math.max(cursor, r.seq);
          const arms = liveArms({ ...status, arms: r.arms });
          liveRows.push({ t: r.t, arms });
        }
        liveRows = liveRows.slice(-2400);
        scene.setData(recentRows(liveRows, 5), lastPoseTime === 0);
        lastPoseTime = cursor;
      }
      if (!frameBusy && mode === "capture") {
        frameBusy = true;
        const version = frameVersion;
        try {
          const results = await Promise.all(
            [...$("videos").children].map((card) =>
              loadImage(
                card,
                "/api/preview/" + encodeURIComponent(card.dataset.stream),
                (status.sources_age_s["image/" + card.dataset.stream] ??
                  Infinity) < 2,
              ),
            ),
          );
          applyImages(results, version);
        } finally {
          frameBusy = false;
        }
      }
    } catch (e) {
      notice("实时视图更新失败：" + e.message, true);
    }
  }
  setTimeout(pollLive, livePeriodMs());
}
// 处理与导出: 采集质量 sits beside the recordings list (right), collapsible and
// resizable; 回放与标注 keeps it in its own place below the playback.
const qualityHome = { parent: $("qualityPanel").parentNode, next: $("qualityPanel").nextSibling },
  QUALITY_WIDTH = 560,
  QUALITY_MIN = 420;
function stored(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private window: the layout just is not remembered */
  }
}
function placeQuality() {
  const panel = $("qualityPanel");
  if (mode === "quality") {
    if (panel.parentNode !== $("qualitySlot")) $("qualitySlot").append(panel);
  } else if (panel.parentNode !== qualityHome.parent)
    qualityHome.parent.insertBefore(panel, qualityHome.next);
  $("qualityCollapse").hidden = mode !== "quality";
  setQualityCollapsed(stored("qualityCollapsed", "0") === "1");
  setQualityWidth(Number(stored("qualityWidth", QUALITY_WIDTH)));
}
function setQualityCollapsed(collapsed) {
  $("exportSplit").classList.toggle("quality-collapsed", collapsed);
  $("qualityExpand").hidden = !collapsed;
  store("qualityCollapsed", collapsed ? "1" : "0");
}
function setQualityWidth(width) {
  const total = $("exportSplit").clientWidth || window.innerWidth;
  const value = Math.round(Math.max(QUALITY_MIN, Math.min(width || QUALITY_WIDTH, total * 0.65)));
  $("exportSplit").style.setProperty("--quality-width", value + "px");
  return value;
}
$("qualityCollapse").onclick = () => setQualityCollapsed(true);
$("qualityExpand").onclick = () => setQualityCollapsed(false);
$("qualityResizer").addEventListener("pointerdown", (e) => {
  e.preventDefault();
  const handle = $("qualityResizer");
  handle.setPointerCapture(e.pointerId);
  handle.classList.add("dragging");
  const move = (ev) => setQualityWidth($("exportSplit").getBoundingClientRect().right - ev.clientX);
  const up = (ev) => {
    handle.releasePointerCapture(ev.pointerId);
    handle.classList.remove("dragging");
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", up);
    store("qualityWidth", parseInt(getComputedStyle($("exportSplit")).getPropertyValue("--quality-width")));
  };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", up);
});
$("qualityResizer").addEventListener("dblclick", () => store("qualityWidth", setQualityWidth(QUALITY_WIDTH)));
$("qualityResizer").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  e.preventDefault();
  const now = parseInt(getComputedStyle($("exportSplit")).getPropertyValue("--quality-width")) || QUALITY_WIDTH;
  store("qualityWidth", setQualityWidth(now + (e.key === "ArrowLeft" ? 20 : -20)));
});
function changeMode() {
  const requested = location.hash.slice(1).split("?")[0];
  mode = ["capture", "review", "quality"].includes(requested)
    ? requested
    : "capture";
  playing = false;
  updatePlay();
  frameVersion++;
  document.querySelectorAll("[data-mode]").forEach((a) => {
    if (a.dataset.mode === mode) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.body.dataset.mode = mode;
  lastRenderedFrame = "";
  $("pageTitle").textContent = {
    capture: "实时采集",
    review: "回放与标注",
    quality: "处理与导出",
  }[mode];
  $("contextHint").textContent = {
    capture: "检查视频、定位和开合度，准备一次完整采集。",
    review: "在同一时间轴检查画面与位姿，划分动作并填写任务。",
    quality: "选择录制和导出内容，生成或追加 LeRobot 训练集。",
  }[mode];
  $("readiness").hidden = mode !== "capture";
  $("devicePanel").hidden = mode !== "capture";
  $("sourceAlerts").hidden =
    mode !== "capture" || !$("sourceAlerts").textContent;
  $("sessionBar").hidden = mode !== "review";
  $("sessionDrawer").hidden = $("drawerToggle").hidden = mode !== "review";
  $("jobDrawer").hidden = $("jobToggle").hidden = mode !== "review";
  if (mode === "review") pollJobs();
  $("poseTools").hidden = mode !== "review";
  $("trailToggle").hidden = mode !== "capture";
  $("clearTrail").hidden = mode !== "capture";
  $("stage").hidden = mode === "quality";
  $("armCards").hidden = mode === "quality";
  $("reviewPanel").hidden = mode !== "review";
  $("qualityPanel").hidden = mode === "capture";
  $("exportPanel").hidden = mode !== "quality";
  $("batchBar").hidden = mode !== "review";
  $("cameraTitle").textContent = mode === "capture" ? "相机画面" : "同步视频";
  scene.setVisible(mode !== "quality");
  lerobot.setVisible(mode === "quality");
  placeQuality();
  if (mode === "capture") {
    videoCards(liveStreams);
    scene.setCameraView(status?.camera_view);
    scene.setData(recentRows(liveRows, 5), true);
    $("sceneEmpty").querySelector("strong").textContent = "等待双臂定位";
  } else {
    videoCards(review?.streams || liveStreams);
    scene.setCameraView(review?.camera_view);
    scene.setData(review?.rows || [], true);
    $("sceneEmpty").querySelector("strong").textContent = review
      ? "此录制暂无有效定位"
      : "选择录制查看 3D 轨迹";
    if (review) seek(currentTime);
    else armCards();
  }
  if (mode === "quality") {
    renderQualitySessions();
    refreshDatasets().catch((e) => notice(e.message, true));
    loadExportCatalog().catch((e) => notice(e.message, true));
  }
  if (mode === "review") {
    pollRecomputeAll();
    pollDraftAll();
  }
  updateControls();
}
function sessionStatusSuffix(r) {
  return r.status !== "stopped" ? " · 未结束" : !r.annotated ? " · 未标注" : "";
}
function sessionTimestamp(r) {
  return r.started_unix_ns
    ? new Date(r.started_unix_ns / 1e6).toLocaleString("zh-CN", { hour12: false })
    : "";
}
function sessionLabel(r) {
  return `${r.name} · ${seconds(r.duration_s, 1)}` + sessionStatusSuffix(r);
}
async function refresh() {
  records = await api("/api/sessions");
  $("sessions").replaceChildren(
    new Option(records.length ? "选择一条录制…" : "暂无录制，请先开始采集", ""),
  );
  for (const r of records) {
    const option = new Option(sessionLabel(r), r.name);
    option.disabled = r.status !== "stopped";
    $("sessions").add(option);
  }
  $("sessions").value = selectedName;
  updateSessionNav();
  renderQualitySessions();
  renderDrawer();
}
// ---- 模型任务队列（右侧，回放与标注） ----
let jobsOpen = true,
  jobTimer = null,
  jobSeen = {}; // "kind:session" -> running, to notice a job of the open recording finish
try {
  jobsOpen = localStorage.getItem("jobDrawer") !== "closed";
} catch {}
function applyJobsDrawer() {
  document.body.classList.toggle("jobs-closed", !jobsOpen);
  const active = jobList.filter((j) => j.running).length;
  $("jobToggle").textContent = (jobsOpen ? "›" : "‹") + (!jobsOpen && active ? ` ${active}` : "");
  $("jobToggle").classList.toggle("busy", active > 0);
}
$("jobToggle").onclick = () => {
  jobsOpen = !jobsOpen;
  try {
    localStorage.setItem("jobDrawer", jobsOpen ? "open" : "closed");
  } catch {}
  applyJobsDrawer();
};
const JOB_KIND = { draft: "预标注", label: "逐段标注", pause: "静止检测" };
function jobState(j) {
  if (j.running && j.cancel) return "取消中…（当前一步做完后停止）";
  if (j.running && j.queued) return `排队中 · 第 ${j.position || "?"} 位`;
  if (j.running) return j.kind === "pause" ? "进行中" : `进行中 ${j.done} / ${j.total || "…"}`;
  const at = j.finished ? new Date(j.finished * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
  if (j.cancelled) return `已取消 ${at}`;
  if (j.error) return `失败 ${at}：${j.error}`;
  return `完成 ${at}` + (j.kind === "label" ? (j.applied ? " · 已处理" : " · 待填入") : "");
}
function renderJobs() {
  const active = jobList.filter((j) => j.running).length;
  $("jobCount").textContent = active ? `${active} 个进行或排队` : "空闲";
  $("jobList").replaceChildren(
    ...(jobList.length
      ? jobList.map((j) => {
          const row = el("div", undefined, "job-item" + (j.running ? (j.queued ? " queued" : " running") : j.error ? " failed" : " done"));
          row.setAttribute("role", "listitem");
          const open = el("button", undefined, "job-open" + (j.session === selectedName ? " active" : ""));
          open.type = "button";
          open.title = "打开这条录制";
          open.append(
            el("strong", j.session),
            el("small", `${JOB_KIND[j.kind] || j.kind}${j.count ? ` · ${j.count} 段` : ""} · ${jobState(j)}`),
          );
          if (j.running && !j.queued && j.total)
            open.append(Object.assign(el("span", undefined, "job-bar"), { style: `--p:${(100 * j.done) / j.total}%` }));
          open.onclick = () => selectSession(j.session).catch((e) => notice(e.message, true));
          row.append(open);
          if (j.running && j.kind !== "pause" && !j.cancel) {
            const cancel = el("button", "取消", "quiet job-cancel");
            cancel.type = "button";
            cancel.onclick = async () => {
              try {
                await api(`/api/jobs/${j.kind}/${encodeURIComponent(j.session)}`, "DELETE");
              } catch (e) {
                notice(e.message, true);
              }
              pollJobs();
            };
            row.append(cancel);
          }
          return row;
        })
      : [el("div", "还没有模型任务。点「预标注」或「模型逐段标注」后会出现在这里。", "empty-state")]),
  );
  applyJobsDrawer();
}
async function pollJobs() {
  clearTimeout(jobTimer);
  jobTimer = null;
  try {
    jobList = (await api("/api/jobs")).jobs;
  } catch {
    jobList = [];
  }
  // A job of the open recording that just finished: bring its result in.
  for (const j of jobList) {
    const key = `${j.kind}:${j.session}`,
      was = jobSeen[key];
    jobSeen[key] = j.running;
    if (!was || j.running || j.session !== selectedName || !review) continue;
    if (j.kind === "label") api(base() + "/label").then((job) => applyLabelJob(job, false)).catch(() => {});
    else if (j.kind === "draft") loadDraft(selectedName, true);
  }
  renderJobs();
  updateModelButtons();
  if (mode === "review") jobTimer = setTimeout(pollJobs, jobList.some((j) => j.running) ? 2000 : 8000);
}
applyJobsDrawer();
// Drag the queue's left edge to resize it, like the recordings list; remembered.
const JOBS_MIN = 200,
  JOBS_MAX = 560,
  JOBS_DEFAULT = 260;
function setJobsWidth(px, save) {
  const w = Math.round(Math.max(JOBS_MIN, Math.min(JOBS_MAX, px)));
  document.documentElement.style.setProperty("--jobs-w", w + "px");
  if (save)
    try {
      localStorage.setItem("jobDrawerWidth", String(w));
    } catch {}
}
try {
  const saved = Number(localStorage.getItem("jobDrawerWidth"));
  if (saved) setJobsWidth(saved, false);
} catch {}
$("jobResize").addEventListener("pointerdown", (e) => {
  e.preventDefault();
  const right = $("jobDrawer").getBoundingClientRect().right,
    handle = $("jobResize");
  handle.setPointerCapture(e.pointerId);
  document.body.classList.add("drawer-resizing");
  const move = (ev) => setJobsWidth(right - ev.clientX, false);
  const up = (ev) => {
    setJobsWidth(right - ev.clientX, true);
    document.body.classList.remove("drawer-resizing");
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", up);
  };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", up);
});
$("jobResize").ondblclick = () => setJobsWidth(JOBS_DEFAULT, true);
// ---- 录制记录侧边栏 ----
let drawerOpen = true,
  drawerSelection = new Set(); // recordings ticked for batch processing
try {
  drawerOpen = localStorage.getItem("sessionDrawer") !== "closed";
} catch {}
function applyDrawer() {
  document.body.classList.toggle("drawer-closed", !drawerOpen);
  $("drawerToggle").textContent = drawerOpen ? "‹" : "›";
}
$("drawerToggle").onclick = () => {
  drawerOpen = !drawerOpen;
  try {
    localStorage.setItem("sessionDrawer", drawerOpen ? "open" : "closed");
  } catch {}
  applyDrawer();
};
$("drawerFilter").oninput = () => renderDrawer();
function drawerShown() {
  const filter = $("drawerFilter").value.trim().toLowerCase();
  return records.filter((r) => !filter || r.name.toLowerCase().includes(filter));
}
$("drawerSelectShown").onclick = () => {
  for (const r of drawerShown()) if (r.status === "stopped") drawerSelection.add(r.name);
  renderDrawer();
};
$("drawerSelectNone").onclick = () => {
  drawerSelection.clear();
  renderDrawer();
};
function updateBatchLabels() {
  const n = drawerSelection.size;
  $("drawerSelected").textContent = n ? `已选 ${n} 条` : "勾选录制可批量处理";
  $("recomputeAll").textContent = n ? `离线重算选中 ${n} 条` : "全部离线重算";
  $("draftAll").textContent = n ? `预标注选中 ${n} 条` : "全部预标注";
}
function renderDrawer() {
  const shown = drawerShown();
  for (const name of [...drawerSelection])
    if (!records.some((r) => r.name === name)) drawerSelection.delete(name);
  $("drawerCount").textContent =
    shown.length < records.length ? `${shown.length} / ${records.length}` : `${records.length} 条`;
  updateBatchLabels();
  $("drawerList").replaceChildren(
    ...shown.map((r) => {
      const b = el("button", undefined, "drawer-item" + (r.name === selectedName ? " active" : ""));
      b.type = "button";
      b.disabled = r.status !== "stopped";
      const tags = el("span", undefined, "tags");
      const tag = (text, cls) => tags.append(el("i", text, cls));
      if (r.status !== "stopped") tag("录制中", "todo");
      else if (r.annotated) tag("已标注");
      else tag(r.drafted ? "有草稿" : "未标注", "todo");
      if (r.pose_offline) tag("已重算");
      if (r.exported_to?.length) tag("已导出");
      b.append(el("strong", r.name), el("small", `${sessionTimestamp(r)} · ${seconds(r.duration_s, 1)}`), tags);
      b.onclick = (e) => {
        if (e.ctrlKey || e.metaKey) return toggle(!drawerSelection.has(r.name));
        selectSession(r.name).catch((err) => notice(err.message, true));
      };
      const row = el("div", undefined, "drawer-row" + (drawerSelection.has(r.name) ? " checked" : "")),
        check = document.createElement("input");
      check.type = "checkbox";
      check.checked = drawerSelection.has(r.name);
      check.disabled = r.status !== "stopped";
      check.setAttribute("aria-label", "勾选批量处理 " + r.name);
      const toggle = (on) => {
        if (on) drawerSelection.add(r.name);
        else drawerSelection.delete(r.name);
        renderDrawer();
      };
      check.onchange = () => toggle(check.checked);
      row.append(check, b);
      return row;
    }),
  );
  revealIn($("drawerList"), $("drawerList").querySelector(".active"));
}
// Drag the drawer's right edge to resize it; the width is remembered.
const DRAWER_MIN = 200,
  DRAWER_MAX = 640,
  DRAWER_DEFAULT = 280;
function setDrawerWidth(px, save) {
  const w = Math.round(Math.max(DRAWER_MIN, Math.min(DRAWER_MAX, px)));
  document.documentElement.style.setProperty("--drawer-w", w + "px");
  if (save)
    try {
      localStorage.setItem("sessionDrawerWidth", String(w));
    } catch {}
}
try {
  const saved = Number(localStorage.getItem("sessionDrawerWidth"));
  if (saved) setDrawerWidth(saved, false);
} catch {}
$("drawerResize").addEventListener("pointerdown", (e) => {
  e.preventDefault();
  const left = $("sessionDrawer").getBoundingClientRect().left,
    handle = $("drawerResize");
  handle.setPointerCapture(e.pointerId);
  document.body.classList.add("drawer-resizing");
  const move = (ev) => setDrawerWidth(ev.clientX - left, false);
  const up = (ev) => {
    setDrawerWidth(ev.clientX - left, true);
    document.body.classList.remove("drawer-resizing");
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", up);
  };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", up);
});
$("drawerResize").ondblclick = () => setDrawerWidth(DRAWER_DEFAULT, true);
applyDrawer();
function stoppedRecords() {
  return records.filter((r) => r.status === "stopped");
}
function updateSessionNav() {
  const stopped = stoppedRecords();
  const index = stopped.findIndex((r) => r.name === selectedName);
  $("prevSession").disabled = index <= 0;
  $("nextSession").disabled = index === -1 || index >= stopped.length - 1;
  $("deleteSession").disabled = index === -1 || reviewBusy || exportBusy;
}
bind("prevSession", async () => {
  const stopped = stoppedRecords();
  const index = stopped.findIndex((r) => r.name === selectedName);
  if (index > 0) await selectSession(stopped[index - 1].name);
});
bind("nextSession", async () => {
  const stopped = stoppedRecords();
  const index = stopped.findIndex((r) => r.name === selectedName);
  if (index !== -1 && index < stopped.length - 1)
    await selectSession(stopped[index + 1].name);
});
async function deleteRecordings(names) {
  const list = names
    .map((name) => records.find((r) => r.name === name))
    .filter(Boolean);
  if (!list.length) return;
  const exported = list.filter((r) => r.exported_to.length).length;
  const ok = await confirmDelete(
    "删除录制",
    (list.length === 1
      ? `将永久删除录制 ${list[0].name}`
      : `将永久删除选中的 ${list.length} 条录制`) +
      " 的原始视频、位姿、开合度和标注，不可撤销。" +
      (exported
        ? `其中 ${exported} 条已导出过，数据集文件夹里的副本不受影响。`
        : "") +
      "确定继续吗？",
  );
  if (!ok) return;
  const deleted = new Set(),
    failed = [];
  for (const r of list) {
    try {
      await api("/api/sessions/" + encodeURIComponent(r.name), "DELETE");
      deleted.add(r.name);
      exportSelection.delete(r.name);
    } catch (e) {
      failed.push(`${r.name}：${e.message}`);
    }
  }
  if (deleted.has(selectedName)) {
    setDirty(false);
    editorDirty = false;
    await selectSession("");
  }
  await refresh();
  notice(
    failed.length
      ? `已删除 ${deleted.size} 条；未删除：${failed.join("；")}`
      : `已删除 ${deleted.size} 条录制`,
    failed.length > 0,
  );
}
bind("deleteSession", () => deleteRecordings([selectedName]));
bind("deleteSelected", () => deleteRecordings([...exportSelection]));
function discardApproval() {
  return new Promise((resolve) => {
    const d = $("discardDialog");
    d.showModal();
    const finish = (value) => {
      d.close();
      resolve(value);
    };
    $("keepEditing").onclick = () => finish(false);
    $("discardChanges").onclick = () => finish(true);
    d.oncancel = () => resolve(false);
  });
}
async function selectSession(name) {
  if (name === selectedName) return;
  if ((dirty || editorDirty) && !(await discardApproval())) {
    $("sessions").value = selectedName;
    return;
  }
  if (dirty && selectedName) {
    // Discarded on purpose: do not offer the stash back next time.
    clearTimeout(wipTimer);
    api(base() + "/wip", "DELETE").catch(() => {});
  }
  playing = false;
  updatePlay();
  frameVersion++;
  selectedName = name;
  $("sessions").value = name;
  renderDrawer();
  editorDirty = false;
  review = null;
  parts = [];
  setDirty(false);
  editing = -1;
  currentTime = 0;
  clearTimeout(poseTimer);
  poseTimer = null;
  clearTimeout(draftTimer);
  draftTimer = null;
  draftInfo = null;
  renderPose(null);
  scene.setData([]);
  scene.cursor(null);
  armCards();
  resetEditor();
  renderParts();
  renderQuality();
  renderQualitySessions();
  for (const card of $("videos").children) loadImage(card, "", false);
  if (!name) {
    renderParts();
    renderQuality();
    updateControls();
    return;
  }
  $("sessionMeta").textContent = "正在加载录制…";
  updateControls();
  const token = name;
  try {
    const data = await api(base() + "/review");
    if (selectedName !== token) return;
    installReview(data);
    loadService();
    // A finished labelling job is offered once the draft is in: its labels
    // go onto segments by their bounds, and its notice must not be replaced.
    loadDraft(name, true)
      .then(() => (name === selectedName ? api(base() + "/label") : null))
      .then((job) => applyLabelJob(job, true))
      .catch(() => {});
    offerWip(name);
    renderJobs();
    $("sessionMeta").textContent =
      `${seconds(data.duration_s, 1)} · ${data.streams.length} 路视频 · ${data.fps} Hz`;
    notice("");
    const url = new URL(location);
    url.searchParams.set("session", name);
    history.replaceState(null, "", url);
  } catch (e) {
    notice(e.message, true);
    $("sessionMeta").textContent = "加载失败，请刷新后重选";
  }
  updateControls();
}
function installReview(data) {
  review = data;
  lastRenderedFrame = "";
  editorDirty = false;
  parts = data.segments.map((p) => ({
    ...p,
    subtask: p.subtask || "",
    task_id: p.task_id || "",
    subtask_id: p.subtask_id || "",
    hands: p.hands || "both",
  }));
  selection = new Set();
  anchor = null;
  review.rows = data.samples.map((row) => ({
    t: row[0],
    arms: {
      left: { ...poseFromState(row, 5, !!row[1]), reason: row[25] },
      right: { ...poseFromState(row, 15, !!row[2]), reason: row[26] },
    },
    grippers: {
      left: { valid: !!row[3], width_m: row[14] },
      right: { valid: !!row[4], width_m: row[24] },
    },
  }));
  setDirty(false);
  $("time").max = data.duration_s;
  $("time").step = 1 / data.fps;
  $("from").step = $("to").step = "any";
  $("to").value = data.duration_s.toFixed(3);
  if (mode !== "capture") videoCards(data.streams);
  renderParts();
  renderQuality();
  renderPose(data.pose);
  if (mode !== "capture") {
    scene.setCameraView(data.camera_view);
    scene.setData(data.rows, true);
  }
  seek(Math.min(currentTime, data.duration_s));
  $("playbackNote").textContent =
    "TCP 与开合度随拖动即时更新；视频画面速率受浏览器解码限制，可能略有滞后";
  $("timelineMarks").replaceChildren(
    ...Array.from({ length: 6 }, (_, i) =>
      el("span", seconds((data.duration_s * i) / 5, 1)),
    ),
  );
}
async function reloadReview() {
  const t = currentTime,
    keep = generated ? parts : null;
  installReview(await api(base() + "/review"));
  if (keep && !parts.length) {
    parts = keep;
    generated = true;
    renderParts();
  }
  seek(t);
}
function renderPose(pose) {
  poseInfo = pose;
  const job = pose?.job,
    running = !!job?.running;
  $("poseSource").value = pose?.source || "live";
  $("poseSource").options[1].disabled = !pose?.offline;
  if (document.activeElement !== $("poseFilter") && !poseFilterTimer)
    $("poseFilter").value = pose ? pose.filter_hz : "";
  $("poseStatus").className = !running && job?.error ? "muted error" : "muted";
  $("poseStatus").textContent = !pose
    ? ""
    : running
      ? job.queued
        ? "离线重算排队中…"
        : `离线重算中 ${job.done} / ${job.total || "…"} 帧`
      : job?.error
        ? `离线重算失败：${job.error}`
        : pose.offline
          ? `离线结果 ${new Date(pose.offline.created_unix_ns / 1e6).toLocaleString("zh-CN", { hour12: false })} · 用时 ${pose.offline.elapsed_s} s`
          : "尚未离线重算";
  if (running && !poseTimer)
    poseTimer = setTimeout(() => pollPose(selectedName), 1000);
  updateControls();
}
async function pollPose(name) {
  poseTimer = null;
  if (name !== selectedName) return;
  let pose;
  try {
    pose = await api(base() + "/pose");
  } catch (e) {
    notice(e.message, true);
    return;
  }
  if (name !== selectedName) return;
  const finished = !!poseInfo?.job?.running && !pose.job?.running;
  if (finished && !pose.job.error && !dirty && !editorDirty) {
    try {
      await reloadReview();
      notice("离线重算完成，已切换为离线轨迹；回放、静止分段与导出都使用此轨迹");
    } catch (e) {
      notice(e.message, true);
    }
    return;
  }
  renderPose(pose);
  if (finished)
    notice(
      pose.job.error
        ? `离线重算失败：${pose.job.error}`
        : "离线重算完成；请先保存标注，再切换到离线轨迹查看",
      !!pose.job.error,
    );
}
async function changePose(values) {
  if (dirty || editorDirty) {
    renderPose(poseInfo);
    notice("请先保存标注，再切换轨迹来源或低通设置", true);
    return;
  }
  reviewBusy = true;
  updateControls();
  try {
    await api(base() + "/pose", "POST", values);
    await reloadReview();
    notice(
      values.source
        ? values.source === "offline"
          ? "已切换为离线重算轨迹"
          : "已切换为实时录制轨迹"
        : values.filter_hz
          ? `低通截止频率已设为 ${values.filter_hz} Hz`
          : "已关闭低通，显示原始位姿",
    );
  } catch (e) {
    notice(e.message, true);
    renderPose(poseInfo);
  } finally {
    reviewBusy = false;
    updateControls();
  }
}
function commitPoseFilter() {
  clearTimeout(poseFilterTimer);
  poseFilterTimer = null;
  const text = $("poseFilter").value,
    value = Number(text);
  if (!poseInfo || text === "" || value === poseInfo.filter_hz) return;
  changePose({ filter_hz: value });
}
$("poseSource").addEventListener("change", () =>
  changePose({ source: $("poseSource").value }),
);
$("poseFilter").addEventListener("change", () => {
  clearTimeout(poseFilterTimer);
  poseFilterTimer = setTimeout(commitPoseFilter, 600);
});
$("poseFilter").addEventListener("keydown", (e) => {
  if (e.key === "Enter") commitPoseFilter();
});
bind("poseRecompute", async () => {
  if (!review) throw Error("请先选择一条已停止的录制");
  renderPose(await api(base() + "/pose/recompute", "POST"));
  notice(
    "已开始离线重算，完成后自动切换为离线轨迹；录制可照常进行，期间重算自动降速",
  );
});
function seek(t) {
  if (!review) return;
  currentTime = Math.max(0, Math.min(Number(t), review.duration_s));
  $("time").value = currentTime;
  $("playTime").textContent =
    `${seconds(currentTime)} / ${seconds(review.duration_s)}`;
  displayPose(currentTime);
  highlightParts();
}
function displayPose(t) {
  if (!review) return;
  const row =
    review.rows[Math.min(review.rows.length - 1, Math.round(t * review.fps))];
  scene.cursor(row);
  armCards(row?.arms, row?.grippers);
  $("frameLabel").textContent = `录制参考系 · ${seconds(t)} · 米`;
}
let replayAbort = null,
  replaySeq = 0;
const replayPending = new WeakSet(),
  replayShown = new WeakMap();
function renderReplay() {
  if (
    review &&
    mode === "review" &&
    !document.hidden &&
    lastRenderedFrame !== selectedName + ":" + currentTime
  ) {
    // Scrubbing: a newer position always wins, so cancel whatever is still
    // in flight. Playing: never cancel — that would leave no frame finished
    // whenever a fetch takes longer than one tick — instead skip a card
    // whose previous frame is still loading and show the newest that lands.
    if (!playing || !replayAbort || replayAbort.signal.aborted) {
      replayAbort?.abort();
      replayAbort = new AbortController();
    }
    const version = frameVersion,
      time = currentTime,
      signal = replayAbort.signal;
    lastRenderedFrame = selectedName + ":" + time;
    for (const card of $("videos").children) {
      card.querySelector(".video-rate").textContent = seconds(time);
      if (playing && replayPending.has(card)) continue;
      const seq = ++replaySeq;
      replayPending.add(card);
      loadImage(
        card,
        base() + "/frame/" + encodeURIComponent(card.dataset.stream) + "?t=" + time,
        true,
        signal,
      ).then((result) => {
        replayPending.delete(card);
        if (!result) return;
        if (version !== frameVersion || seq < (replayShown.get(card) || 0)) {
          if (result.object) URL.revokeObjectURL(result.object);
          return;
        }
        replayShown.set(card, seq);
        applyImages([result], version);
        // Stopped while this card was still catching up: redraw every card
        // at the exact paused time.
        if (!playing && time !== currentTime) lastRenderedFrame = "";
      });
    }
  }
  setTimeout(renderReplay, 40);
}
let playbackClock = performance.now();
function tick(now) {
  if (playing && review && mode === "review" && !document.hidden) {
    seek(
      currentTime + ((now - playbackClock) / 1000) * Number($("speed").value),
    );
    if (currentTime >= review.duration_s) {
      playing = false;
      updatePlay();
    }
  }
  playbackClock = now;
  requestAnimationFrame(tick);
}
function updatePlay() {
  $("play").textContent = playing ? "Ⅱ 暂停" : "▶ 播放";
}
function resetEditor() {
  editing = -1;
  editorDirty = false;
  $("editorTitle").textContent = "添加动作片段";
  $("add").textContent = "添加片段";
  $("cancelEdit").hidden = true;
  $("formError").textContent = "";
  $("task").placeholder = "例如：左手拿起杯子，交给右手…";
  highlightParts();
  renderGuide();
}
// `keepTime`: leave the playhead where it is (boundary moved to it) instead of
// jumping to the segment's start.
function editPart(i, keepTime = false) {
  const p = parts[i];
  editing = i;
  editorDirty = false;
  $("from").value = p.start_s;
  $("to").value = p.end_s;
  $("task").value = p.task;
  $("subtask").value = p.subtask || "";
  fillTaskOptions(p.task_id || "");
  fillSubtaskOptions(p.subtask_id || "");
  $("hands").value = p.hands || "both";
  $("editorTitle").textContent = "编辑动作片段";
  $("add").textContent = "更新片段";
  $("cancelEdit").hidden = false;
  $("formError").textContent = "";
  $("task").placeholder = unfilled(p)
    ? "填写后按回车：更新本段并跳到下一段未填写"
    : "例如：左手拿起杯子，交给右手…";
  if (!keepTime) seek(p.start_s);
  renderGuide();
  revealRow(i);
}
// Bring segment row `i` into view inside the list's own scroll box only:
// scrollIntoView would also scroll the page, pulling the view away from the
// video and timeline the segment was picked on.
function revealRow(i) {
  revealIn($("segments"), $("segments").querySelector(`.segment-row[data-index="${i}"]`));
}
function revealIn(list, row) {
  if (!list || !row) return;
  const box = list.getBoundingClientRect(),
    r = row.getBoundingClientRect();
  if (r.top < box.top) list.scrollTop -= box.top - r.top;
  else if (r.bottom > box.bottom) list.scrollTop += r.bottom - box.bottom;
}
function unfilled(p) {
  return !p?.task?.trim();
}
const LOW_CONFIDENCE = 0.6,
  SHORT_SEGMENT_S = 0.3;
// Checks computed from the list itself (never stored): a very short segment is
// often a mis-cut, and within one task a subtask that comes before the previous
// one in the action table, other than the group starting over, is out of order.
function hints(p, i) {
  const out = [];
  if (p.end_s - p.start_s < SHORT_SEGMENT_S)
    out.push(`片段短于 ${SHORT_SEGMENT_S} 秒，可能是误切，考虑合并（M / Shift+M）`);
  const before = parts[i - 1],
    task = tableTask(p.task_id);
  if (task && before && before.task_id === p.task_id && p.subtask_id && before.subtask_id) {
    const at = task.subtasks.findIndex((x) => x.id === p.subtask_id),
      prev = task.subtasks.findIndex((x) => x.id === before.subtask_id);
    if (at >= 0 && prev >= 0 && at < prev && at !== 0)
      out.push(`顺序存疑：动作表中「${task.subtasks[at].text}」应在「${task.subtasks[prev].text}」之前`);
  }
  return out;
}
// A segment still waiting for a person: no task yet, flagged by the model or
// the checks, or labelled with low model confidence. Opening it and confirming
// (Enter / 更新片段) writes a fresh segment without flags or confidence.
function needsReview(p, i) {
  return (
    unfilled(p) ||
    !!p.flags?.length ||
    hints(p, i).length > 0 ||
    (p.confidence != null && p.confidence < LOW_CONFIDENCE)
  );
}
// Next segment to review: by time after `after` (wrapping), or, with
// 按把握排序 on, the least confident one left.
function nextReview(after = -Infinity, skip = -1) {
  const pending = parts
    .map((p, i) => i)
    .filter((i) => i !== skip && needsReview(parts[i], i));
  if (!pending.length) return -1;
  if ($("reviewByConfidence")?.checked)
    return pending.reduce((a, b) =>
      (parts[b].confidence ?? 0) < (parts[a].confidence ?? 0) ? b : a,
    );
  return pending.find((i) => parts[i].start_s > after) ?? pending[0];
}
function focusReview(after, skip) {
  const i = nextReview(after, skip);
  if (i < 0) {
    notice("没有待复核的片段了，确认无误后点「保存全部标注」");
    return false;
  }
  editPart(i);
  return true;
}
// Enter outside a text field: confirm the open segment as it stands and open
// the next one to review.
function confirmAndNext() {
  if (!review || reviewBusy) return;
  if (editing < 0) {
    focusReview(currentTime);
    return;
  }
  const at = parts[editing].start_s,
    p = commitEditor();
  if (!p) return;
  focusReview(at, parts.indexOf(p));
}
// Digit keys pick the n-th subtask of the chosen task, or the n-th task when
// none is chosen yet.
function pickByNumber(n) {
  if (editing < 0 || !actionTable.tasks.length) return;
  const task = tableTask($("taskId").value);
  if (task) {
    const sub = task.subtasks[n - 1];
    if (!sub) return;
    $("subtaskId").value = sub.id;
    $("subtaskId").onchange();
  } else {
    const t = actionTable.tasks[n - 1];
    if (!t) return;
    $("taskId").value = t.id;
    $("taskId").onchange();
  }
}
// [ / ] move the open segment's start / end to the playhead, pushing the
// neighbour's edge along so the two never overlap.
function edgeToPlayhead(start) {
  if (!review || reviewBusy) return;
  if (editorDirty) {
    notice("请先更新正在编辑的片段，再用 [ / ] 调整边界", true);
    return;
  }
  const i =
    editing >= 0
      ? editing
      : parts.findIndex((p) => currentTime >= p.start_s && currentTime < p.end_s);
  if (i < 0) {
    notice("先打开一个片段，或把播放头移到片段内", true);
    return;
  }
  const p = parts[i],
    t = Math.round(currentTime * 1000) / 1000,
    prev = parts[i - 1],
    next = parts[i + 1];
  if (start ? t >= p.end_s : t <= p.start_s) {
    notice(start ? "起点须早于本段终点" : "终点须晚于本段起点", true);
    return;
  }
  if (start && prev && t <= prev.start_s) {
    notice("会吞掉整个上一段：请先合并（M）", true);
    return;
  }
  if (!start && next && t >= next.end_s) {
    notice("会吞掉整个下一段：请先合并（Shift+M）", true);
    return;
  }
  const previous = parts.map((x) => ({ ...x }));
  if (start) {
    p.start_s = t;
    if (prev && prev.end_s > t) prev.end_s = t;
  } else {
    p.end_s = t;
    if (next && next.start_s < t) next.start_s = t;
  }
  setDirty(true);
  renderParts();
  editPart(i, true);
  offerUndo(start ? "撤销调整起点" : "撤销调整终点", previous);
}
// ---- 模型逐段标注 / 中文转英文 -------------------------------------------
const hasChinese = (p) => CJK.test(p.task || "") || CJK.test(p.subtask || "");
// Which segments 模型逐段标注 works on: the picked ones (relabelled), else
// those without a task yet.
function labelTargets() {
  return selection.size ? selectedParts() : parts.filter(unfilled);
}
function updateModelButtons() {
  const configured = !!service?.configured,
    up = !!service?.health,
    targets = review ? labelTargets() : [],
    chinese = parts.filter(hasChinese).length;
  $("labelParts").hidden = !configured || !review;
  // This recording's own labelling job, if one is queued or running.
  const mine = jobList.find((j) => j.kind === "label" && j.session === selectedName && j.running);
  $("labelParts").disabled = !up || reviewBusy || labelBusy || !!mine || !targets.length;
  $("labelParts").textContent = mine
    ? mine.queued
      ? `逐段标注排队中（第 ${mine.position || "?"} 位）`
      : `逐段标注中 ${mine.done} / ${mine.total || "…"}`
    : selection.size
      ? `模型标注选中的 ${targets.length} 段`
      : `模型标注空白的 ${targets.length} 段`;
  $("labelParts").title = mine
    ? "这条录制的逐段标注在右侧「模型任务」队列里，完成后自动填入"
    : !targets.length
      ? "所有片段都已填写：先 Ctrl / Shift 点击选中要重标的片段"
      : "分段不动，让模型逐段填写 task / subtask：有选中的片段（Ctrl / Shift 点击）就标选中的并覆盖，否则只标还没填写的。加入右侧队列，不用等待";
  $("translateParts").hidden = !configured || !review || !chinese;
  $("translateParts").disabled = !up || reviewBusy || translateBusy;
  $("translateParts").textContent = translateBusy ? "转换中…" : `中文转英文（${chinese} 段）`;
  $("translateForm").hidden =
    !configured || !(CJK.test($("task").value) || CJK.test($("subtask").value));
}
bind("labelParts", async () => {
  if (editorDirty) throw Error("请先添加或更新正在编辑的片段，再让模型标注");
  const targets = labelTargets();
  if (!targets.length) throw Error("没有要标注的片段：先选中片段，或留空任务文本");
  // The task the person already chose for most segments; else the model names it.
  const counts = {};
  for (const p of parts) if (p.task_id) counts[p.task_id] = (counts[p.task_id] || 0) + 1;
  const taskId = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0] || "";
  labelBusy = true;
  updateModelButtons();
  try {
    const job = await api(base() + "/label", "POST", {
      segments: targets.map((p) => ({ start_s: p.start_s, end_s: p.end_s })),
      task_id: taskId,
      // Labels already on the page: the one before each group is the model's context.
      context: parts
        .filter((p) => !targets.includes(p) && p.subtask)
        .map((p) => ({ start_s: p.start_s, end_s: p.end_s, subtask: p.subtask })),
    });
    notice(
      `已把 ${targets.length} 段加入右侧「模型任务」队列` +
        (job.queued ? "，前面还有任务在跑" : "") +
        "；可以继续标别的录制，完成后回到这条录制会填入结果。",
    );
    await pollJobs();
  } finally {
    labelBusy = false;
    updateModelButtons();
  }
});
// A finished labelling job of the open recording: fill its labels in (only
// segments whose bounds still match), once. `offer`: ask first instead.
async function applyLabelJob(job, offer) {
  if (!job || job.running || job.applied || job.error || !job.labels || job.session !== selectedName) return;
  const name = selectedName;
  const acknowledge = () => api(base() + "/label/applied", "POST").catch(() => {});
  const apply = () => {
    if (name !== selectedName) return;
    const previous = parts.map((p) => ({ ...p })),
      near = (a, b) => Math.abs(a - b) < 2e-3;
    let applied = 0;
    for (const label of job.labels) {
      const p = parts.find((x) => near(x.start_s, label.start_s) && near(x.end_s, label.end_s));
      if (!p) continue;
      Object.assign(p, {
        task: label.task || p.task,
        task_id: label.task ? label.task_id : p.task_id,
        subtask: label.subtask || p.subtask,
        subtask_id: label.subtask ? label.subtask_id : p.subtask_id,
        confidence: label.confidence ?? null,
        flags: label.flags || [],
      });
      applied++;
    }
    acknowledge();
    job.applied = true;
    if (!applied) {
      notice("模型逐段标注已完成，但这些片段的起止已经改过，没有可填入的");
      return;
    }
    setDirty(true);
    resetEditor();
    renderParts();
    notice(`模型已标注 ${applied} 段（标为待复核）：按 Enter 逐段核对，全部确认后点「保存全部标注」。`);
    offerUndo("撤销模型标注", previous);
  };
  if (!offer && !editorDirty) return apply();
  notice(`这条录制的模型逐段标注已完成（${job.labels.length} 段）。`);
  const yes = el("button", "填入结果", "quiet"),
    no = el("button", "不用了", "quiet");
  yes.onclick = () => (editorDirty ? notice("请先更新正在编辑的片段", true) : apply());
  no.onclick = () => {
    acknowledge();
    job.applied = true;
    notice("");
  };
  $("message").append(yes, no);
}
async function translateItems(items) {
  const reply = await api("/api/annotation/translate", "POST", { items });
  return reply.items;
}
bind("translateParts", async () => {
  if (editorDirty) throw Error("请先添加或更新正在编辑的片段，再转换");
  const targets = parts.filter(hasChinese);
  if (!targets.length) return;
  // Each distinct (task, subtask) pair once: drafts repeat the same text a lot.
  const keys = [...new Set(targets.map((p) => JSON.stringify([p.task || "", p.subtask || ""])))];
  translateBusy = true;
  updateModelButtons();
  notice(`正在把 ${targets.length} 段（${keys.length} 种）中文标注转成英文…`);
  try {
    const results = await translateItems(keys.map((k) => ({ task: JSON.parse(k)[0], subtask: JSON.parse(k)[1] })));
    const byKey = Object.fromEntries(keys.map((k, i) => [k, results[i]]));
    const previous = parts.map((p) => ({ ...p }));
    for (const p of targets) {
      const r = byKey[JSON.stringify([p.task || "", p.subtask || ""])];
      if (!r) continue;
      p.task = r.task;
      p.subtask = r.subtask;
      if (r.task_id) p.task_id = r.task_id;
      if (r.subtask_id) p.subtask_id = r.subtask_id;
      if (r.flags?.length) p.flags = [...new Set([...(p.flags || []), ...r.flags])];
    }
    setDirty(true);
    resetEditor();
    renderParts();
    const left = parts.filter(hasChinese).length;
    notice(`已转换 ${targets.length - left} 段` + (left ? `，${left} 段仍含中文（悬停“存疑”查看原因）` : "") + "，保存后生效。");
    offerUndo("撤销中文转英文", previous);
  } finally {
    translateBusy = false;
    updateModelButtons();
  }
});
bind("translateForm", async () => {
  translateBusy = true;
  updateModelButtons();
  try {
    const [r] = await translateItems([{ task: $("task").value, subtask: $("subtask").value }]);
    if (r.task_id) fillTaskOptions(r.task_id);
    if (r.subtask_id) fillSubtaskOptions(r.subtask_id);
    $("task").value = r.task;
    $("subtask").value = r.subtask;
    editorDirty = true;
    notice(
      r.flags?.length
        ? "部分没能转成英文：" + r.flags.join("；")
        : "已转成英文" + (r.subtask_id || r.task_id ? "，并对上了动作表条目" : "") + "：确认后点「更新片段」",
    );
  } finally {
    translateBusy = false;
    updateModelButtons();
  }
});
for (const id of ["task", "subtask"]) $(id).addEventListener("input", () => updateModelButtons());
// B: the first segment boundary after the playhead moves back onto it;
// Shift+B: the last one before it moves forward. A boundary shared by two
// touching segments moves for both; it may not pass the far edge of either
// neighbour (that would swallow a segment: merge first).
function nearestBoundaryToPlayhead(before = false) {
  if (!review || reviewBusy) return;
  if (editorDirty) {
    notice("请先更新正在编辑的片段，再移动分隔点", true);
    return;
  }
  if (!parts.length) {
    notice("还没有片段，没有可移动的分隔点", true);
    return;
  }
  const t = Math.round(currentTime * 1000) / 1000,
    // 2 ms: drafts written before both ends were rounded alike are 1 ms apart.
    near = (a, b) => Math.abs(a - b) < 2e-3;
  // Each boundary: the segment ending there (left) and / or starting there (right).
  const bounds = [];
  parts.forEach((p, i) => {
    const touching = i > 0 && near(parts[i - 1].end_s, p.start_s);
    if (!touching) bounds.push({ at: p.start_s, left: -1, right: i });
    const next = parts[i + 1];
    if (next && near(p.end_s, next.start_s)) bounds.push({ at: p.end_s, left: i, right: i + 1 });
    else bounds.push({ at: p.end_s, left: i, right: -1 });
  });
  const side = bounds.filter((x) => (before ? x.at < t - 2e-3 : x.at > t + 2e-3));
  if (!side.length) {
    notice(before ? "播放头之前没有分隔点" : "播放头之后没有分隔点", true);
    return;
  }
  const b = side.reduce((best, x) => (Math.abs(x.at - t) < Math.abs(best.at - t) ? x : best));
  const L = parts[b.left],
    R = parts[b.right],
    frame = 1 / (review.fps || 30),
    lo = L ? L.start_s : (parts[b.right - 1]?.end_s ?? 0),
    hi = R ? R.end_s : (parts[b.left + 1]?.start_s ?? review.duration_s);
  if (!(t > lo + (L ? frame : 0) - 1e-9 && t < hi - (R ? frame : 0) + 1e-9)) {
    notice(
      `${before ? "之前" : "之后"}最近的分隔点在 ${seconds(b.at)}，移到 ${seconds(t)} 会越过相邻片段的另一端：请先合并（M / Shift+M）或把播放头移近一些`,
      true,
    );
    return;
  }
  const previous = parts.map((x) => ({ ...x }));
  if (L) L.end_s = t;
  if (R) R.start_s = t;
  setDirty(true);
  renderParts();
  if (editing >= 0) editPart(editing, true);
  else highlightParts();
  notice(`已把${before ? "之前" : "之后"} ${seconds(b.at)} 处的分隔点${before ? "后移" : "前移"}到 ${seconds(t)}`);
  offerUndo("撤销移动分隔点", previous);
}
// One colour per subtask on the timeline, so mis-cuts and gaps stand out.
function labelHue(p) {
  const key = p.subtask_id || p.subtask || p.task || "";
  let h = 0;
  for (const c of key) h = (h * 31 + c.codePointAt(0)) % 360;
  return h;
}
// Next segment without a task, searching forward from `after` (seconds) and
// wrapping around; -1 when every segment is filled.
function nextUnfilled(after = -Infinity) {
  const i = parts.findIndex((p) => unfilled(p) && p.start_s > after);
  return i >= 0 ? i : parts.findIndex(unfilled);
}
function focusUnfilled(after) {
  const i = nextUnfilled(after);
  if (i < 0) return false;
  editPart(i);
  $("task").focus({ preventScroll: true });
  return true;
}
// Three independent states: unfilled (amber, set when rendering), active =
// open in the editor, current = under the playhead.
function highlightParts() {
  for (const node of document.querySelectorAll(
    "#segments .segment-row, #segmentTrack button",
  )) {
    const i = Number(node.dataset.index),
      p = parts[i];
    node.classList.toggle("active", i === editing);
    node.classList.toggle("selected", selection.has(p));
    node.classList.toggle(
      "current",
      !!p && currentTime >= p.start_s && currentTime < p.end_s,
    );
  }
  updateMerge();
  updateModelButtons();
}
function updateMerge() {
  const picked = selectedParts().length;
  $("mergeParts").disabled = !review || reviewBusy || picked < 2;
  $("mergeParts").textContent = picked >= 2 ? `合并 ${picked} 段` : "合并";
}
function selectedParts() {
  return parts.filter((p) => selection.has(p));
}
// Plain click opens the segment in the editor; Ctrl/Cmd toggles it in the
// merge selection and Shift selects the range from the last clicked one.
function pickPart(i, e) {
  const p = parts[i];
  if (e.ctrlKey || e.metaKey) {
    if (!selection.delete(p)) selection.add(p);
    anchor = p;
  } else if (e.shiftKey && parts.includes(anchor)) {
    const a = parts.indexOf(anchor);
    selection = new Set(parts.slice(Math.min(a, i), Math.max(a, i) + 1));
  } else {
    selection = new Set([p]);
    anchor = p;
    editPart(i);
  }
  highlightParts();
}
function renderGuide() {
  $("segmentGuide").hidden = !review;
  $("save").classList.remove("attention");
  $("fillAll").hidden = true;
  if (!review) return;
  const total = parts.length,
    missing = parts.filter(unfilled).length,
    step = !total ? 1 : missing ? (editing >= 0 ? 3 : 2) : dirty || generated ? 4 : 5;
  [...$("guideSteps").children].forEach((li, i) => {
    li.className = i + 1 < step ? "done" : i + 1 === step ? "current" : "";
  });
  $("guideBar").style.width = total
    ? `${((total - missing) / total) * 100}%`
    : "0";
  const current = parts[editing];
  $("guideText").textContent =
    step === 1
      ? "点「静止分段」或「预标注」生成片段草稿，或拖动时间轴、用「设为当前」设置起止后添加"
      : step === 2
        ? `已填写 ${total - missing} / ${total} 段，还有 ${missing} 段未填写任务（虚线标出）`
        : step === 3
          ? unfilled(current)
            ? `正在填写第 ${editing + 1} 段：播放核对后输入任务，按回车进入下一段（剩 ${missing} 段）`
            : `正在修改第 ${editing + 1} 段，改完点「更新片段」；还有 ${missing} 段未填写`
          : step === 4
            ? `全部 ${total} 段已填写，点「保存全部标注」后生效`
            : `${total} 段标注已保存`;
  $("nextUnfilled").hidden = !missing;
  $("save").classList.toggle("attention", step === 4);
  const others = parts.filter((p, i) => unfilled(p) && i !== editing).length;
  if (editing >= 0 && others) {
    $("fillAll").hidden = false;
    $("fillAll").textContent = `把此任务文本也填到其余 ${others} 段未填写片段`;
  }
}
function renderParts() {
  $("segments").replaceChildren();
  $("segmentTrack").replaceChildren();
  const outside = parts.filter(offTable).length;
  const flagged = parts.filter((p) => p.flags?.length).length,
    pending = parts.filter(needsReview).length;
  $("segmentCount").textContent =
    `${parts.length} 个片段` +
    (pending ? ` · ${pending} 段待复核` : "") +
    (flagged ? ` · ${flagged} 段存疑` : "") +
    (outside ? ` · ${outside} 段未关联动作表` : "");
  if (!parts.length)
    $("segments").append(
      el("div", "拖动时间轴，设置起止点，添加第一个动作片段。", "empty-state"),
    );
  parts.forEach((p, i) => {
    const row = el("div", undefined, "segment-row"),
      b = el("button");
    row.dataset.index = i;
    row.classList.toggle("unfilled", unfilled(p));
    b.type = "button";
    // The subtask is what the row is about; the task it belongs to goes below.
    const heading = el("p", p.subtask || p.task || "未填写任务");
    if (offTable(p)) {
      const tag = el("span", "表外", "off-table");
      tag.title = p.task_id
        ? `task_id ${p.task_id} 不在当前动作表中`
        : "未关联动作表中的 task";
      heading.append(tag);
    }
    if (p.flags?.length) {
      const tag = el("span", "存疑", "off-table flagged");
      tag.title = p.flags.join("\n");
      heading.append(tag);
    }
    const warnings = hints(p, i);
    if (warnings.length) {
      const tag = el("span", "提示", "off-table hint");
      tag.title = warnings.join("\n");
      heading.append(tag);
    }
    if (p.confidence != null) {
      const tag = el(
        "span",
        "把握 " + p.confidence.toFixed(2),
        "off-table confidence" + (p.confidence < LOW_CONFIDENCE ? " low" : ""),
      );
      tag.title = "模型自评把握（0–1），低于 " + LOW_CONFIDENCE + " 计入待复核";
      heading.append(tag);
    }
    b.append(
      heading,
      el(
        "small",
        `#${i + 1} · ${seconds(p.start_s)} — ${seconds(p.end_s)} · ${title(p.hands || "both")}`,
      ),
    );
    if (p.subtask && p.task) b.append(el("small", "task：" + p.task, "subtask"));
    if (p.note) b.append(el("small", "模型：" + p.note, "note"));
    if (p.task_id)
      b.append(
        el(
          "small",
          "动作表：" + p.task_id + (p.subtask_id ? " / " + p.subtask_id : ""),
          "subtask",
        ),
      );
    b.onclick = (e) => pickPart(i, e);
    const remove = el("button", "移除", "quiet");
    remove.type = "button";
    remove.onclick = () => {
      const removed = parts.splice(i, 1)[0];
      setDirty(true);
      resetEditor();
      renderParts();
      notice("片段已移除，保存后生效。");
      const undo = el("button", "撤销", "quiet");
      undo.onclick = () => {
        parts.push(removed);
        parts.sort((a, b) => a.start_s - b.start_s);
        renderParts();
        notice("已撤销移除");
      };
      $("message").append(undo);
    };
    row.append(b, remove);
    $("segments").append(row);
    if (review) {
      // Fine-grained drafts share one task text, so the subtask tells them apart.
      const block = el("button", p.subtask || p.task || "未填写", p.hands || "both");
      block.dataset.index = i;
      block.classList.toggle("unfilled", unfilled(p));
      if (!unfilled(p)) block.style.setProperty("--seg-hue", labelHue(p));
      block.classList.toggle("hued", !unfilled(p));
      block.classList.toggle("pending", needsReview(p, i));
      block.style.left = (p.start_s / review.duration_s) * 100 + "%";
      block.style.width =
        ((p.end_s - p.start_s) / review.duration_s) * 100 + "%";
      block.title = `#${i + 1} ${p.task || "未填写任务"}${p.subtask ? " / " + p.subtask : ""}：${seconds(p.start_s)} 至 ${seconds(p.end_s)}`;
      block.onclick = (e) => pickPart(i, e);
      $("segmentTrack").append(block);
    }
  });
  highlightParts();
  renderGuide();
  renderQuality();
}
function metricLabel(key) {
  return (
    {
      "left.position": "左手位置",
      "left.rotation": "左手姿态",
      "left.gripper": "左手开合度",
      "right.position": "右手位置",
      "right.rotation": "右手姿态",
      "right.gripper": "右手开合度",
      all_required: "全部必需维度",
    }[key] || (key.startsWith("video.") ? title(key.slice(6)) : key)
  );
}
function jumpMissing(t) {
  location.hash = "review";
  seek(t);
}
const PAIRED_QUALITY_METRICS = [
  ["left.position", "right.position", "位置"],
  ["left.rotation", "right.rotation", "姿态"],
  ["left.gripper", "right.gripper", "开合度"],
  ["video.rgb_left_up", "video.rgb_right_up", "上侧 RGB"],
  ["video.rgb_left_down", "video.rgb_right_down", "下侧 RGB"],
];
function qualityBar(v) {
  const bar = el("div", undefined, "bar"),
    fill = el("i");
  fill.style.width = v * 100 + "%";
  if (v < 0.95) fill.style.background = "#dfb27d";
  bar.append(fill);
  return bar;
}
function gapButton(gaps) {
  const jump = el("button", gaps.length ? "检查缺失" : "无缺失", "quiet");
  jump.disabled = !gaps.length;
  jump.onclick = () => jumpMissing(gaps[0][0]);
  return jump;
}
function qualitySingleRow(key, v, q) {
  const row = el("div", undefined, "quality-row" + (v < 0.95 ? " low" : ""));
  row.append(
    el("span", metricLabel(key)),
    qualityBar(v),
    el("strong", (v * 100).toFixed(1) + "%"),
    gapButton(q.missing_intervals[key] || []),
  );
  return row;
}
function qualityPairRow(label, leftKey, leftV, rightKey, rightV, q) {
  // Per-side "low" coloring is applied directly on each bar by qualityBar();
  // the row itself skips .low so the shared `.quality-row.low .bar i` rule
  // doesn't force the OTHER (fine) side's bar amber too.
  const row = el("div", undefined, "quality-row paired");
  const gaps = [...(q.missing_intervals[leftKey] || []), ...(q.missing_intervals[rightKey] || [])].sort(
    (a, b) => a[0] - b[0],
  );
  row.append(
    el("span", label),
    el("span", "左", "side-tag left"),
    qualityBar(leftV),
    el("strong", (leftV * 100).toFixed(1) + "%"),
    el("span", "右", "side-tag right"),
    qualityBar(rightV),
    el("strong", (rightV * 100).toFixed(1) + "%"),
    gapButton(gaps),
  );
  return row;
}
// Quality scope: all segments (default), one pinned segment, or the whole recording.
let qualityScope = "segments",
  qualitySegment = 0;
function mergeRanges(ranges) {
  const out = [];
  for (const [a, b] of [...ranges].sort((x, y) => x[0] - y[0])) {
    const last = out[out.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else out.push([a, b]);
  }
  return out;
}
// The report restricted to `ranges` (seconds): missing intervals clipped to
// them and every valid ratio recomputed over their total length.
function scopeQuality(q, ranges) {
  ranges = mergeRanges(ranges);
  const span = ranges.reduce((n, [a, b]) => n + b - a, 0),
    missing = {},
    ratios = { ...q.valid_ratios };
  for (const [key, intervals] of Object.entries(q.missing_intervals)) {
    const clipped = [];
    for (const [a, b] of intervals)
      for (const [lo, hi] of ranges) {
        const x = Math.max(a, lo),
          y = Math.min(b, hi);
        if (y > x) clipped.push([x, y]);
      }
    clipped.sort((x, y) => x[0] - y[0]);
    missing[key] = clipped;
    if (key in ratios)
      ratios[key] = span
        ? Math.max(0, 1 - clipped.reduce((n, [a, b]) => n + b - a, 0) / span)
        : 0;
  }
  return { ...q, valid_ratios: ratios, missing_intervals: missing, span_s: span };
}
function partName(p, i) {
  return `#${i + 1} ${p.subtask || p.task || "未填写"}`;
}
// The weakest metric of a scoped report, for the per-segment list.
function weakest(sq) {
  let worst = null;
  for (const [key, v] of Object.entries(sq.valid_ratios))
    if (key !== "all_required" && (!worst || v < worst[1])) worst = [key, v];
  return worst;
}
for (const b of document.querySelectorAll(".scope-switch button"))
  b.onclick = () => {
    if (b.dataset.scope === "segment" && qualityScope !== "segment" && editing >= 0)
      qualitySegment = editing;
    qualityScope = b.dataset.scope;
    renderQuality();
  };
$("qualitySegment").onchange = () => {
  qualitySegment = Number($("qualitySegment").value);
  renderQuality();
};
function renderSegmentQuality(q, segs) {
  $("segmentQuality").replaceChildren(
    ...segs.map(([p, i]) => {
      const sq = scopeQuality(q, [[p.start_s, p.end_s]]),
        v = sq.valid_ratios.all_required,
        worst = weakest(sq),
        b = el("button", undefined, "segment-quality-row" + (v < 0.95 ? " low" : ""));
      b.type = "button";
      b.classList.toggle("active", qualityScope === "segment" && i === qualitySegment);
      b.append(
        el("span", partName(p, i)),
        el("small", `${seconds(p.start_s)} — ${seconds(p.end_s)}`),
        qualityBar(v),
        el("strong", (v * 100).toFixed(1) + "%"),
        el("small", worst && worst[1] < 1 ? `最低：${metricLabel(worst[0])} ${(worst[1] * 100).toFixed(0)}%` : "全部有效"),
      );
      b.onclick = () => {
        qualityScope = "segment";
        qualitySegment = i;
        seek(p.start_s);
        renderQuality();
      };
      return b;
    }),
  );
}
function renderQuality() {
  const full = review?.quality;
  $("quality").replaceChildren();
  $("qualitySummary").replaceChildren();
  $("dimensions").replaceChildren();
  $("validityTrack").replaceChildren();
  $("segmentQualityPanel").hidden = true;
  $("qualitySegment").hidden = true;
  if (!full) {
    $("quality").append(
      el(
        "div",
        mode === "quality"
          ? "在下方录制列表点「查看质量」，或在回放页选择录制。"
          : "选择一条录制，查看质量报告。",
        "empty-state",
      ),
    );
    return;
  }
  const segs = parts.map((p, i) => [p, i]).filter(([p]) => p.end_s > p.start_s);
  let scope = qualityScope;
  if (scope !== "video" && !segs.length) scope = "video";
  qualitySegment = Math.min(Math.max(0, qualitySegment), Math.max(0, parts.length - 1));
  for (const b of document.querySelectorAll(".scope-switch button")) {
    b.classList.toggle("active", b.dataset.scope === scope);
    b.setAttribute("aria-checked", String(b.dataset.scope === scope));
    b.disabled = b.dataset.scope !== "video" && !segs.length;
  }
  const pinned = parts[qualitySegment];
  const q =
    scope === "video"
      ? full
      : scopeQuality(
          full,
          scope === "segment"
            ? [[pinned.start_s, pinned.end_s]]
            : segs.map(([p]) => [p.start_s, p.end_s]),
        );
  $("qualityScopeNote").textContent =
    scope === "video"
      ? (qualityScope !== "video" ? "这条录制还没有分段，显示全部视频；" : "") +
        "按整次录制的预期帧数评估；无效数据不会补齐成有效帧。"
      : scope === "segments"
        ? `只统计 ${segs.length} 个分段覆盖的时间（${dirty ? "含未保存的修改" : "已保存的分段"}），分段之外的时段不计入。`
        : `只统计 ${partName(pinned, qualitySegment)}（${seconds(pinned.start_s)} — ${seconds(pinned.end_s)}）。`;
  if (scope === "segment") {
    $("qualitySegment").hidden = false;
    $("qualitySegment").replaceChildren(
      ...segs.map(([p, i]) => new Option(`${partName(p, i)} · ${seconds(p.start_s)}`, String(i))),
    );
    $("qualitySegment").value = String(qualitySegment);
  }
  if (scope !== "video") {
    $("segmentQualityPanel").hidden = false;
    renderSegmentQuality(full, segs);
  }
  $("dimensionsBox").hidden = scope !== "video";
  const span = scope === "video" ? q.duration_s : q.span_s;
  const metrics = [
    [
      "完整有效率",
      (q.valid_ratios.all_required * 100).toFixed(1) + "%",
      "全部必需维度同时有效",
    ],
    scope === "video"
      ? ["预期采样帧", q.expected_frames.toLocaleString(), "包含缺失时段"]
      : ["分段采样帧", Math.round(span * q.fps).toLocaleString(), scope === "segments" ? `${segs.length} 个分段合计` : "本分段"],
    [scope === "video" ? "录制时长" : "分段时长", seconds(span, 1), `${q.fps} Hz 统一采样`],
    [
      "可导出帧",
      q.export_plan.frames.toLocaleString(),
      `${q.export_plan.episodes} 个连续片段（按已保存分段）`,
    ],
  ];
  for (const [label, value, sub] of metrics) {
    const d = el("div", undefined, "quality-stat");
    d.append(el("span", label), el("strong", value), el("small", sub));
    $("qualitySummary").append(d);
  }
  const ratios = q.valid_ratios,
    handled = new Set();
  for (const [leftKey, rightKey, label] of PAIRED_QUALITY_METRICS) {
    if (!(leftKey in ratios) || !(rightKey in ratios)) continue;
    handled.add(leftKey);
    handled.add(rightKey);
    $("quality").append(
      qualityPairRow(label, leftKey, ratios[leftKey], rightKey, ratios[rightKey], q),
    );
  }
  for (const [key, v] of Object.entries(ratios)) {
    if (handled.has(key)) continue;
    $("quality").append(qualitySingleRow(key, v, q));
  }
  for (const [key, v] of Object.entries(q.state_dimension_valid_ratios)) {
    const d = el("div");
    d.append(el("span", key), el("span", (v * 100).toFixed(1) + "%"));
    $("dimensions").append(d);
  }
  for (const [start, end] of full.missing_intervals.all_required) {
    const b = el("button");
    b.style.left = (start / full.duration_s) * 100 + "%";
    b.style.width =
      (Math.min(end - start, full.duration_s - start) / full.duration_s) * 100 + "%";
    b.title = `缺失 ${seconds(start)} 至 ${seconds(Math.min(end, full.duration_s))}`;
    b.setAttribute("aria-label", b.title);
    b.onclick = () => seek(start);
    $("validityTrack").append(b);
  }
}
function renderQualitySessions() {
  const list = $("qualitySessions");
  list.replaceChildren();
  const stopped = stoppedRecords();
  if (!stopped.length) {
    list.append(el("div", "暂无已完成的录制。", "empty-state"));
    updateBatchExport();
    return;
  }
  for (const r of stopped) {
    const row = el(
      "div",
      undefined,
      "quality-session-row" + (r.name === selectedName ? " active" : ""),
    );
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = exportSelection.has(r.name);
    check.setAttribute("aria-label", "选中导出 " + r.name);
    check.onclick = (e) => e.stopPropagation();
    check.onchange = () => {
      if (check.checked) exportSelection.add(r.name);
      else exportSelection.delete(r.name);
      updateBatchExport();
    };
    const info = el("div", undefined, "quality-session-info");
    info.append(
      el("strong", r.name),
      el(
        "small",
        `${sessionTimestamp(r)} · ${seconds(r.duration_s, 1)}` +
          sessionStatusSuffix(r) +
          (r.pose_offline ? " · 已离线重算" : "") +
          (r.drafted && !r.annotated ? " · 有预标注草稿" : "") +
          (r.exported_to.length
            ? ` · 已导出到：${r.exported_to.join(", ")}`
            : " · 尚未导出"),
      ),
    );
    const view = el("button", "查看质量", "quiet");
    view.type = "button";
    view.onclick = (e) => {
      e.stopPropagation();
      selectSession(r.name)
        .then(() => $("qualityPanel").scrollIntoView({ behavior: "smooth", block: "start" }))
        .catch((err) => notice(err.message, true));
    };
    row.append(check, info, view);
    row.onclick = () => {
      check.checked = !check.checked;
      check.onchange();
    };
    list.append(row);
  }
  updateBatchExport();
}
function updateBatchExport() {
  updateSelectionCount();
  $("batchExport").disabled =
    exportSelection.size === 0 || exportBusy || !!status?.recording;
  $("deleteSelected").disabled = exportSelection.size === 0 || exportBusy;
}
$("datasetTarget").onchange = () => {
  $("newDatasetName").hidden = $("datasetTarget").value !== "__new__";
  adoptDatasetViews($("datasetTarget").value).catch((e) => notice(e.message, true));
};
async function refreshDatasets() {
  datasets = await api("/api/datasets");
  const select = $("datasetTarget"),
    previous = select.value;
  select.replaceChildren(new Option("+ 新建文件夹…", "__new__"));
  for (const d of datasets)
    select.add(new Option(`${d.name}（${d.episode_count} 段）`, d.name));
  select.value = datasets.some((d) => d.name === previous) ? previous : "__new__";
  $("newDatasetName").hidden = select.value !== "__new__";
  lerobot.setDatasets(datasets);
  renderDatasetFolders();
}
function renderDatasetFolders() {
  const wrap = $("datasetFolders");
  wrap.replaceChildren();
  if (!datasets.length) {
    wrap.append(el("div", "还没有导出过的数据集文件夹。", "empty-state"));
    return;
  }
  for (const d of datasets) {
    const row = el("div", undefined, "dataset-folder-row");
    const info = el("div", undefined, "dataset-folder-info");
    info.append(
      el("strong", d.name),
      el(
        "small",
        `${d.episode_count} 个片段 · ${d.total_frames} 帧 · ${d.session_count} 条录制 · ${(d.size_bytes / 1e6).toFixed(1)} MB`,
      ),
    );
    const actions = el("div", undefined, "dataset-folder-actions");
    const view = el("button", datasetDetailName === d.name ? "收起" : "查看", "quiet");
    view.type = "button";
    view.onclick = () => toggleDatasetDetail(d.name);
    const play = el("button", "回放", "quiet");
    play.type = "button";
    play.onclick = () => lerobot.open(d.name);
    const remove = el("button", "删除文件夹", "quiet");
    remove.type = "button";
    remove.onclick = () => deleteDatasetFolder(d.name);
    actions.append(view, play, remove);
    row.append(info, actions);
    wrap.append(row);
  }
}
async function loadDatasetDetail(name) {
  $("datasetDetail").replaceChildren(el("p", "加载中…", "muted"));
  try {
    renderDatasetDetail(await api("/api/datasets/" + encodeURIComponent(name)));
  } catch (e) {
    $("datasetDetail").replaceChildren(el("p", e.message, "error-text"));
  }
}
function renderDatasetDetail(detail) {
  $("datasetDetail").replaceChildren();
  $("datasetDetail").append(el("h4", `${detail.name} 内的录制`));
  if (!detail.sessions.length) {
    $("datasetDetail").append(el("div", "空文件夹。", "empty-state"));
    return;
  }
  for (const s of detail.sessions) {
    const row = el("div", undefined, "dataset-session-row");
    row.append(
      el("span", s.source_session),
      el("span", `${s.episodes} 段 · ${s.frames} 帧`, "muted"),
    );
    const remove = el("button", "删除此录制", "quiet");
    remove.type = "button";
    remove.onclick = () => deleteDatasetSession(detail.name, s.source_session);
    row.append(remove);
    $("datasetDetail").append(row);
  }
}
async function toggleDatasetDetail(name) {
  if (datasetDetailName === name) {
    datasetDetailName = "";
    $("datasetDetail").hidden = true;
    renderDatasetFolders();
    return;
  }
  datasetDetailName = name;
  $("datasetDetail").hidden = false;
  renderDatasetFolders();
  await loadDatasetDetail(name);
}
function confirmDelete(title, body) {
  return new Promise((resolve) => {
    const d = $("deleteDialog");
    $("deleteDialogTitle").textContent = title;
    $("deleteDialogBody").textContent = body;
    d.showModal();
    const finish = (value) => {
      d.close();
      resolve(value);
    };
    $("confirmDelete").onclick = () => finish(true);
    $("cancelDelete").onclick = () => finish(false);
    d.oncancel = () => resolve(false);
  });
}
async function deleteDatasetFolder(name) {
  if (
    !(await confirmDelete(
      "删除数据集文件夹",
      `将永久删除文件夹 ${name} 及其全部导出数据，且无法撤销，确定继续吗？`,
    ))
  )
    return;
  try {
    await api("/api/datasets/" + encodeURIComponent(name), "DELETE");
    if (datasetDetailName === name) {
      datasetDetailName = "";
      $("datasetDetail").hidden = true;
    }
    notice(`已删除数据集文件夹 ${name}`);
    await refreshDatasets();
  } catch (e) {
    notice(e.message, true);
  }
}
async function deleteDatasetSession(datasetName, sessionName) {
  if (
    !(await confirmDelete(
      "删除这条录制的导出数据",
      `将从 ${datasetName} 中移除 ${sessionName} 贡献的全部片段，且无法撤销，确定继续吗？`,
    ))
  )
    return;
  try {
    await api(
      `/api/datasets/${encodeURIComponent(datasetName)}/sessions/${encodeURIComponent(sessionName)}`,
      "DELETE",
    );
    notice(`已从 ${datasetName} 移除 ${sessionName}`);
    await refresh();
    await refreshDatasets();
    if (datasetDetailName === datasetName) await loadDatasetDetail(datasetName);
  } catch (e) {
    notice(e.message, true);
  }
}
// Arrow/spin steps fire `change` on every press; commit once the user pauses
// instead of racing one request per step against the status poll.
let sampleRateTimer = null;
async function commitSampleRate() {
  clearTimeout(sampleRateTimer);
  sampleRateTimer = null;
  const input = $("sampleRate"),
    fps = Number(input.value);
  if (!Number.isInteger(fps) || fps < 1 || fps > 120) {
    notice("采样率须为 1–120 的整数", true);
    if (status) input.value = status.fps;
    sampleRateEdit.until = 0;
    return;
  }
  if (status && fps === status.fps) {
    sampleRateEdit.until = 0;
    return;
  }
  sampleRateEdit.pending = true;
  try {
    await api("/api/settings", "POST", { fps });
    sampleRateEdit.committedAt = performance.now();
    if (status) status.fps = fps;
    notice(`采样率已改为 ${fps} Hz，下次开始录制起生效`);
  } catch (e) {
    notice(e.message, true);
    if (status) input.value = status.fps;
  } finally {
    sampleRateEdit.pending = false;
    sampleRateEdit.until = 0;
  }
}
$("sampleRate").addEventListener("input", () => {
  sampleRateEdit.until = performance.now() + 1500;
});
$("sampleRate").addEventListener("change", () => {
  sampleRateEdit.until = performance.now() + 1500;
  clearTimeout(sampleRateTimer);
  sampleRateTimer = setTimeout(commitSampleRate, 600);
});
$("sampleRate").addEventListener("keydown", (e) => {
  if (e.key === "Enter") commitSampleRate();
});
$("sampleRate").addEventListener("blur", () => {
  if (sampleRateTimer) commitSampleRate();
});
$("vioEnabled").addEventListener("change", async () => {
  const input = $("vioEnabled"),
    vioEnabled = input.checked;
  try {
    await api("/api/settings", "POST", { vio_enabled: vioEnabled });
    notice(
      vioEnabled
        ? "已启用 VIO，下次开始录制起生效"
        : "已关闭 VIO，位姿将按固定相机计算，下次开始录制起生效",
    );
  } catch (e) {
    notice(e.message, true);
    if (status) input.checked = status.vio_enabled;
  }
});
bind("start", async () => {
  const name = $("recordName").value.trim();
  if (name && !/^[A-Za-z0-9_-]+$/.test(name))
    throw Error("录制名称只能包含字母、数字、下划线和短横线");
  actionBusy = true;
  try {
    await api("/api/start", "POST", { name });
    notice("已开始录制");
    await refresh();
  } finally {
    actionBusy = false;
  }
});
bind("stop", async () => {
  actionBusy = true;
  try {
    const r = await api("/api/stop", "POST");
    notice("录制已保存，可进入回放与标注。");
    await refresh();
    if (r.stopped) {
      location.hash = "review";
      await selectSession(r.stopped);
      $("sessions").value = r.stopped;
    }
  } finally {
    actionBusy = false;
  }
});
bind("refresh", refresh);
bind("refreshQualitySessions", refresh);
bind("refreshDatasets", refreshDatasets);
$("sessions").onchange = () =>
  selectSession($("sessions").value).catch((e) => notice(e.message, true));
bind("play", () => {
  playing = !playing;
  if (playing && currentTime >= review.duration_s) seek(0);
  playbackClock = performance.now();
  updatePlay();
});
bind("prevFrame", () => {
  playing = false;
  updatePlay();
  seek(currentTime - 1 / review.fps);
});
bind("nextFrame", () => {
  playing = false;
  updatePlay();
  seek(currentTime + 1 / review.fps);
});
$("time").oninput = () => {
  playing = false;
  updatePlay();
  frameVersion++;
  seek($("time").value);
};
$("markStart").onclick = () => {
  $("from").value = currentTime.toFixed(3);
  editorDirty = true;
};
$("markEnd").onclick = () => {
  $("to").value = currentTime.toFixed(3);
  editorDirty = true;
};
$("cancelEdit").onclick = resetEditor;
$("segmentForm").addEventListener("input", (e) => {
  // Picking an action-table file or typing the service address is not an
  // edit of the open segment.
  if (
    e.target === $("actionFile") ||
    e.target === $("serviceUrl") ||
    e.target.closest("#segParams")
  )
    return;
  // Labels apply to the open segment as typed; only times wait for 更新片段.
  if (
    editing >= 0 &&
    ["task", "subtask", "taskId", "subtaskId", "hands"].includes(e.target.id)
  ) {
    applyLabels();
    return;
  }
  editorDirty = true;
});
// Validate the editor and write it into `parts`; returns the stored part, or
// null with the reason shown under the form.
function commitEditor() {
  if (!review || reviewBusy) return null;
  const p = {
    start_s: Number($("from").value),
    end_s: Number($("to").value),
    task: $("task").value.trim(),
    subtask: $("subtask").value.trim(),
    task_id: $("taskId").value,
    subtask_id: $("subtaskId").value,
    hands: $("hands").value,
  };
  const others = parts.filter((_, i) => i !== editing);
  let error = "";
  if (!p.task) error = "请填写任务文本";
  else if (
    !(
      p.start_s >= 0 &&
      p.start_s < p.end_s &&
      p.end_s <= review.duration_s + 1e-6
    )
  )
    error = "起止时间须在录制范围内，且终点晚于起点";
  else if (others.some((o) => p.start_s < o.end_s && p.end_s > o.start_s))
    error = "片段与已有标注重叠，请调整起止时间";
  if (error) {
    $("formError").textContent = error;
    (p.task ? $("from") : $("task")).focus({ preventScroll: true });
    return null;
  }
  if (editing >= 0) parts[editing] = p;
  else parts.push(p);
  parts.sort((a, b) => a.start_s - b.start_s);
  setDirty(true);
  resetEditor();
  renderParts();
  return p;
}
$("segmentForm").onsubmit = (e) => {
  e.preventDefault();
  const reviewing = editing >= 0 && needsReview(parts[editing], editing),
    p = commitEditor();
  if (p && reviewing) focusReview(p.start_s, parts.indexOf(p));
};
$("nextUnfilled").onclick = () => {
  const from = editing >= 0 ? parts[editing].start_s : currentTime;
  // Keep what was typed for the open segment instead of dropping it.
  if (editorDirty && editing >= 0 && !commitEditor()) return;
  focusUnfilled(from);
};
$("fillAll").onclick = () => {
  const task = $("task").value.trim(),
    taskId = $("taskId").value;
  if (!task) {
    $("formError").textContent = "请先在上面填写任务文本";
    $("task").focus({ preventScroll: true });
    return;
  }
  const previous = parts.map((p) => ({ ...p }));
  if (editing >= 0 && !commitEditor()) return;
  let count = 0;
  for (const p of parts)
    if (unfilled(p)) {
      p.task = task;
      p.task_id = taskId;
      count++;
    }
  setDirty(true);
  resetEditor();
  renderParts();
  notice(
    `已把“${task}”同时填到其余 ${count} 段；可点任一段单独修改，确认后保存全部标注`,
  );
  const undo = el("button", "撤销批量填写", "quiet");
  undo.type = "button";
  undo.onclick = () => {
    parts = previous;
    setDirty(true);
    resetEditor();
    renderParts();
    notice("已撤销批量填写");
  };
  $("message").append(undo);
};
// Restore `previous` (a copy of parts taken before an edit) from the notice.
function offerUndo(label, previous) {
  const undo = el("button", label, "quiet");
  undo.type = "button";
  undo.onclick = () => {
    parts = previous;
    selection = new Set();
    setDirty(true);
    resetEditor();
    renderParts();
    notice("已" + label);
  };
  $("message").append(undo);
}
$("moveBoundary").onclick = (e) => nearestBoundaryToPlayhead(e.shiftKey);
$("splitPart").onclick = () => {
  if (!review || reviewBusy) return;
  if (editorDirty) {
    notice("请先添加或更新正在编辑的片段，再分隔", true);
    return;
  }
  const t = Number(currentTime.toFixed(3)),
    frame = 1 / review.fps,
    i = parts.findIndex(
      (p) => t - p.start_s >= frame && p.end_s - t >= frame,
    );
  if (i < 0) {
    notice("请把播放位置移到要分隔的片段内部（离起止点至少 1 帧）", true);
    return;
  }
  const previous = parts.map((p) => ({ ...p })),
    p = parts[i];
  parts.splice(
    i,
    1,
    { ...p, end_s: t },
    {
      start_s: t,
      end_s: p.end_s,
      task: "",
      subtask: "",
      task_id: "",
      subtask_id: "",
      hands: "both",
    },
  );
  selection = new Set();
  setDirty(true);
  resetEditor();
  renderParts();
  editPart(i + 1);
  notice(
    `已在 ${seconds(t)} 把第 ${i + 1} 段分成两段：前段保留原属性，后段待填写，保存后生效。`,
  );
  offerUndo("撤销分隔", previous);
};
$("mergeParts").onclick = () => {
  if (!review || reviewBusy) return;
  if (editorDirty) {
    notice("请先添加或更新正在编辑的片段，再合并", true);
    return;
  }
  const picked = selectedParts();
  if (picked.length < 2) return;
  const first = parts.indexOf(picked[0]),
    last = parts.indexOf(picked[picked.length - 1]);
  if (last - first + 1 !== picked.length) {
    notice("只能合并相邻的片段：中间不能隔着未选中的片段", true);
    return;
  }
  const previous = parts.map((p) => ({ ...p }));
  parts.splice(first, picked.length, {
    ...parts[first],
    end_s: parts[last].end_s,
  });
  selection = new Set();
  setDirty(true);
  resetEditor();
  renderParts();
  notice(
    `已把第 ${first + 1}–${last + 1} 段合并为一段，保留第 ${first + 1} 段的属性，保存后生效。`,
  );
  offerUndo("撤销合并", previous);
};
// M merges the current segment (open in the editor, else under the playhead)
// into the previous one, Shift+M with the next one. The merged segment keeps
// the current segment's labels when it has a task, else the neighbour's.
function mergeNeighbour(forward) {
  if (!review || reviewBusy) return;
  if (editorDirty) {
    notice("请先更新正在编辑的片段的起止时间，再合并", true);
    return;
  }
  const i =
    editing >= 0
      ? editing
      : parts.findIndex((p) => currentTime >= p.start_s && currentTime < p.end_s);
  if (i < 0) {
    notice("播放头不在任何片段上：先点选一段，或把播放头移到片段内", true);
    return;
  }
  const j = forward ? i - 1 : i + 1;
  if (j < 0 || j >= parts.length) {
    notice(forward ? "已是第一段，前面没有可合并的片段" : "已是最后一段，后面没有可合并的片段", true);
    return;
  }
  const previous = parts.map((p) => ({ ...p })),
    [first, last] = forward ? [j, i] : [i, j],
    keep = unfilled(parts[i]) ? parts[j] : parts[i];
  const merged = {
    ...keep,
    start_s: parts[first].start_s,
    end_s: parts[last].end_s,
  };
  parts.splice(first, 2, merged);
  selection = new Set();
  setDirty(true);
  resetEditor();
  renderParts();
  editPart(first);
  notice(
    `已把第 ${first + 1}、${last + 1} 段合并（${forward ? "向前" : "向后"}），保存后生效。`,
  );
  offerUndo("撤销合并", previous);
}
function tableTask(id) {
  return id ? actionTable.tasks.find((t) => t.id === id) : undefined;
}
// Filled segments with no task id, or one the current table no longer has.
function offTable(p) {
  return actionTable.tasks.length > 0 && !unfilled(p) && !tableTask(p.task_id);
}
function entryLabel(x) {
  return (x.zh ? `${x.zh} · ` : "") + x.text;
}
// Rebuild a select from `entries`, keeping `value` selectable even when the
// table no longer has it so editing an old segment does not drop its id.
function fillSelect(select, entries, value, blank) {
  select.replaceChildren(el("option", blank));
  select.firstChild.value = "";
  for (const x of entries) {
    const option = el("option", entryLabel(x));
    option.value = x.id;
    option.title = [x.id, x.description].filter(Boolean).join("：");
    select.append(option);
  }
  if (value && !entries.some((x) => x.id === value)) {
    const option = el("option", `${value}（当前动作表中已无）`);
    option.value = value;
    select.append(option);
  }
  select.value = value;
  select.hidden = !actionTable.tasks.length && !value;
}
function fillTaskOptions(value = $("taskId").value) {
  fillSelect(
    $("taskId"),
    actionTable.tasks,
    value,
    "— 从动作表选择 task（不选则为表外动作）—",
  );
}
function fillSubtaskOptions(value = $("subtaskId").value) {
  const task = tableTask($("taskId").value);
  fillSelect(
    $("subtaskId"),
    task?.subtasks || [],
    value,
    task ? "— 从动作表选择 subtask（可不选）—" : "— 先选择 task —",
  );
  $("subtaskId").disabled = reviewBusy || !task;
}
function renderActionTable() {
  const tasks = actionTable.tasks,
    subtasks = tasks.reduce((n, t) => n + t.subtasks.length, 0);
  $("actionTableInfo").textContent = tasks.length
    ? `动作表 ${actionTable.version} · ${tasks.length} 个 task · ${subtasks} 个 subtask`
    : "未导入动作表，task / subtask 手动填写";
  $("actionTableInfo").title = actionTable.imported_at
    ? `导入于 ${actionTable.imported_at}，来源 ${actionTable.source || "未知"}`
    : "";
  fillTaskOptions();
  fillSubtaskOptions();
  renderParts();
}
// Picking an entry always writes its text into the box, replacing whatever was
// typed: the pick is the annotator's latest choice.
function prefill(input, entry) {
  if (entry) input.value = entry.text;
}
// The segment open in the editor follows its task / subtask / hands fields as
// they change: no "更新片段" click needed. Start and end still go through
// 更新片段, which checks them against the other segments.
function applyLabels() {
  if (editing < 0 || !parts[editing] || !review || reviewBusy) return;
  const p = parts[editing],
    next = {
      task: $("task").value.trim(),
      subtask: $("subtask").value.trim(),
      task_id: $("taskId").value,
      subtask_id: $("subtaskId").value,
      hands: $("hands").value,
    };
  if (Object.keys(next).every((k) => (p[k] || "") === next[k])) return;
  Object.assign(p, next);
  setDirty(true);
  const keep = editing;
  renderParts();
  editing = keep;
  highlightParts();
  renderGuide();
}
$("taskId").onchange = () => {
  const task = tableTask($("taskId").value);
  prefill($("task"), task);
  if (!task?.subtasks.some((x) => x.id === $("subtaskId").value)) {
    // The old subtask belonged to another task; drop its text too.
    $("subtask").value = "";
    fillSubtaskOptions("");
  } else {
    fillSubtaskOptions();
    // Same subtask id, but this task may word it differently.
    prefill(
      $("subtask"),
      task.subtasks.find((x) => x.id === $("subtaskId").value),
    );
  }
  applyLabels();
};
$("subtaskId").onchange = () => {
  const task = tableTask($("taskId").value);
  prefill(
    $("subtask"),
    task?.subtasks.find((x) => x.id === $("subtaskId").value),
  );
  applyLabels();
};
$("importActions").onclick = () => $("actionFile").click();
$("actionFile").onchange = async () => {
  const file = $("actionFile").files[0];
  $("actionFile").value = "";
  if (!file) return;
  try {
    const bytes = await file.arrayBuffer();
    let content;
    try {
      content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      // CSV saved from Excel on a Chinese Windows is GBK, not UTF-8.
      content = new TextDecoder("gb18030").decode(bytes);
    }
    actionTable = await api("/api/action-table", "POST", {
      filename: file.name,
      content,
    });
    renderActionTable();
    const outside = parts.filter(offTable).length;
    notice(
      `已导入动作表 ${actionTable.version}（${actionTable.tasks.length} 个 task），所有录制共用` +
        (outside ? `；当前录制有 ${outside} 段未关联动作表` : ""),
    );
  } catch (e) {
    notice("导入动作表失败：" + e.message, true);
  }
};
// Pre-annotation: the service status, this session's draft and its job.
async function loadService() {
  try {
    service = await api("/api/annotation/service");
  } catch {
    service = null;
  }
  renderDraft();
}
function renderDraft() {
  const job = draftInfo?.job,
    draft = draftInfo?.draft,
    running = !!job?.running,
    configured = !!service?.configured,
    up = !!service?.health;
  let text;
  if (!review) text = "预标注：选择录制后显示";
  else if (running)
    text =
      job.stage === "pause"
        ? "录制已结束，静止检测中…"
        : job.queued
          ? "预标注排队中…"
          : `预标注中 ${job.done} / ${job.total || "…"} 段`;
  else if (job?.error) text = "预标注失败：" + job.error;
  else if (draft?.source === "pause")
    text =
      `静止分段草稿 ${draft.segments.length} 段（任务待填写）` +
      (up ? " · 可点「预标注」生成任务文本" : "") +
      " · " +
      new Date(draft.created_unix_ns / 1e6).toLocaleString("zh-CN", { hour12: false });
  else if (draft) {
    const flagged = draft.segments.filter((s) => s.flags?.length).length;
    text =
      `预标注草稿 ${draft.segments.length} 段` +
      (flagged ? ` · ${flagged} 段存疑` : "") +
      (draft.task?.task_id ? ` · task ${draft.task.task_id}` : "") +
      ` · ${draft.model || "模型未知"} · ` +
      new Date(draft.created_unix_ns / 1e6).toLocaleString("zh-CN", {
        hour12: false,
      });
  } else if (!service) text = "预标注：检查推理服务…";
  else if (!configured) text = "未配置推理服务，不做预标注（点「服务设置」填写地址）";
  else if (!up) text = `推理服务 ${service.url} 连不上，录制停止后不会预标注`;
  else text = "这条录制还没有预标注草稿";
  $("draftInfo").textContent = text;
  $("draftInfo").title = service?.url ? "推理服务 " + service.url : "";
  $("loadDraft").hidden = !draft || running;
  $("loadDraft").disabled = reviewBusy;
  $("runDraft").hidden = !configured;
  $("runDraft").disabled = !review || reviewBusy || running || !up;
  $("runDraft").textContent = draft && draft.source !== "pause" ? "重新预标注" : "预标注";
  updateModelButtons();
  $("runDraft").title = up
    ? `用推理服务（${service.health.model}）重新生成这条录制的预标注草稿`
    : `推理服务未启动或连不上：${service?.url || ""}`;
}
// `apply`: install the draft when the session has no segments yet.
async function loadDraft(name, apply) {
  clearTimeout(draftTimer);
  draftTimer = null;
  let state;
  try {
    state = await api(base() + "/draft");
  } catch {
    return;
  }
  if (name !== selectedName) return;
  const finished = !!draftInfo?.job?.running && !state.job?.running;
  draftInfo = state;
  renderDraft();
  if (state.job?.running) {
    draftTimer = setTimeout(() => loadDraft(name, false), 2000);
    return;
  }
  if (finished && state.job.error) {
    notice("预标注失败：" + state.job.error, true);
    return;
  }
  if (!state.draft || !(apply || finished)) return;
  if (!parts.length && !dirty && !editorDirty) applyDraft(true);
  else if (finished) notice("预标注完成，可点「载入草稿」替换当前片段");
}
function applyDraft(auto) {
  const draft = draftInfo?.draft;
  if (!draft) return;
  const previous = parts.map((p) => ({ ...p }));
  installGenerated(draft.segments.map((s) => ({
    start_s: s.start_s,
    end_s: s.end_s,
    task: s.task || "",
    subtask: s.subtask || "",
    task_id: s.task_id || "",
    subtask_id: s.subtask_id || "",
    hands: s.hands || "both",
    note: s.description || "",
    flags: s.flags || [],
    confidence: s.confidence ?? null,
  })));
  selection = new Set();
  resetEditor();
  renderParts();
  const flagged = parts.filter((p) => p.flags.length).length;
  notice(
    draft.source === "pause"
      ? `${auto ? "已自动载入" : "已载入"}录制结束时静止检测的分段草稿 ${parts.length} 段：逐段填写任务，全部填完后点「保存全部标注」才生效。`
      : `${auto ? "已自动载入" : "已载入"}预标注草稿 ${parts.length} 段` +
          (flagged ? `，其中 ${flagged} 段存疑（红色标记）` : "") +
          "。逐段核对，打开并「更新片段」即视为确认；全部核对后点「保存全部标注」才生效。",
  );
  offerUndo("撤销载入草稿", previous);
}
$("serviceSettings").onclick = () => {
  const open = $("serviceForm").hidden;
  $("serviceForm").hidden = !open;
  if (open) {
    $("serviceUrl").value = service?.url || "";
    $("serviceUrl").focus();
  }
};
$("serviceUrl").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault(); // would submit the segment form
  $("saveService").click();
});
bind("saveService", async () => {
  service = await api("/api/annotation/service", "POST", {
    url: $("serviceUrl").value,
  });
  $("serviceUrl").value = service.url;
  renderDraft();
  if (!service.url) notice("已清空推理服务地址：之后的录制不再预标注");
  else if (service.health) {
    $("serviceForm").hidden = true;
    notice(`已连接推理服务 ${service.url}（${service.health.model}）`);
  } else
    notice(
      `已保存 ${service.url}，但目前连不上；服务启动后，新的录制会自动预标注`,
      true,
    );
});
$("loadDraft").onclick = () => {
  if (editorDirty) {
    notice("请先添加或更新正在编辑的片段，再载入草稿", true);
    return;
  }
  applyDraft(false);
};
bind("runDraft", async () => {
  draftInfo = await api(base() + "/draft", "POST");
  renderDraft();
  notice("已开始预标注，完成后可载入草稿");
  draftTimer = setTimeout(() => loadDraft(selectedName, false), 1500);
});
bind("save", async () => {
  if (editorDirty) throw Error("请先添加或更新正在编辑的片段，再保存全部标注");
  const missing = parts.filter(unfilled).length;
  if (missing) {
    focusUnfilled();
    throw Error(`还有 ${missing} 段未填写任务，已定位到第 ${editing + 1} 段`);
  }
  reviewBusy = true;
  updateControls();
  try {
    await api(
      base() + "/segments",
      "POST",
      parts.map(({ note, flags, confidence, ...p }) => p),
    );
    const t = currentTime;
    const data = await api(base() + "/review");
    installReview(data);
    seek(t);
    notice("标注已保存");
  } finally {
    reviewBusy = false;
    updateControls();
  }
});
bind("autoSegment", async () => {
  if (!review) throw Error("请先选择一条已停止的录制");
  if (editorDirty) throw Error("请先添加或更新正在编辑的片段，再静止分段");
  reviewBusy = true;
  updateControls();
  let suggestions;
  try {
    suggestions = await api(base() + "/segments/auto");
  } finally {
    // Re-enable the form before focusing the task box below.
    reviewBusy = false;
    updateControls();
  }
  const previous = parts,
    wasDirty = dirty,
    wasGenerated = generated;
  installGenerated(suggestions);
  resetEditor();
  renderParts();
  focusUnfilled();
  notice(
    suggestions.length
      ? `已按双臂静止检测生成 ${suggestions.length} 个片段草稿，并打开第 1 段：输入任务后按回车依次填写，全部填完再保存。`
      : "没有检测到可用片段：录制中可能缺少连续有效的位姿，请手动添加。",
  );
  const undo = el("button", "撤销静止分段", "quiet");
  undo.type = "button";
  undo.onclick = () => {
    parts = previous;
    setDirty(wasDirty);
    generated = wasGenerated;
    resetEditor();
    renderParts();
    notice("已撤销静止分段");
  };
  $("message").append(undo);
});
bind("qc", async () => {
  if (dirty || editorDirty) throw Error("请先保存标注，再重新评估");
  reviewBusy = true;
  updateControls();
  try {
    const data = await api(base() + "/review");
    installReview(data);
    notice("质量报告已更新");
  } finally {
    reviewBusy = false;
    updateControls();
  }
});
// ---- 处理与导出: export image picker, selection helpers -------------------
let exportCatalog = null,
  exportChoice = new Map(), // view id -> { on, key }
  recomputeAllTimer = null,
  draftAllTimer = null;
async function loadExportCatalog() {
  exportCatalog = await api("/api/export/catalog");
  if (!exportChoice.size)
    for (const v of exportCatalog.views)
      exportChoice.set(v.id, { on: v.default, key: v.key });
  $("handCropM").value = $("handCropM").value || exportCatalog.hand_crop_m;
  $("exportFields").replaceChildren(
    ...exportCatalog.fields.map((f) => {
      const li = el("li");
      li.append(el("code", f.key), el("span", " " + f.label));
      return li;
    }),
  );
  renderExportViews();
}
function renderExportViews() {
  const wrap = $("exportViews");
  wrap.replaceChildren();
  if (!exportCatalog) return;
  let group = "";
  for (const v of exportCatalog.views) {
    if (v.group !== group) {
      group = v.group;
      wrap.append(el("h4", group));
    }
    const choice = exportChoice.get(v.id),
      row = el("div", undefined, "export-view-row" + (choice.on ? " on" : "")),
      check = document.createElement("input"),
      key = document.createElement("input");
    check.type = "checkbox";
    check.checked = choice.on;
    check.setAttribute("aria-label", "导出 " + v.label);
    check.onchange = () => {
      choice.on = check.checked;
      renderExportViews();
    };
    key.value = choice.key;
    key.disabled = !choice.on;
    key.setAttribute("aria-label", v.label + " 的视频名称");
    key.oninput = () => {
      choice.key = key.value.trim();
      updateExportSummary();
    };
    const info = el("div", undefined, "export-view-info");
    info.append(el("strong", v.label), el("small", v.description));
    row.append(check, info, key);
    wrap.append(row);
  }
  updateExportSummary();
}
function selectedViews() {
  return exportCatalog.views
    .filter((v) => exportChoice.get(v.id).on)
    .map((v) => ({ id: v.id, key: exportChoice.get(v.id).key }));
}
function updateExportSummary() {
  if (!exportCatalog) return;
  const views = selectedViews();
  $("exportViewsSummary").textContent = `· ${views.length} 路图像：${views.map((v) => v.key).join("、") || "未选择"}`;
}
// Appending to an existing dataset needs the same videos: adopt its set.
async function adoptDatasetViews(name) {
  if (!exportCatalog || name === "__new__") return;
  const detail = await api(`/api/datasets/${encodeURIComponent(name)}`);
  if (!detail.video_keys?.length) return;
  for (const choice of exportChoice.values()) choice.on = false;
  const missing = [];
  for (const key of detail.video_keys) {
    const id =
      detail.video_views?.[key]?.view ||
      (key === "head" ? "head_raw" : `rgb_raw:${key}`);
    const choice = exportChoice.get(id);
    if (choice) Object.assign(choice, { on: true, key });
    else missing.push(key);
  }
  renderExportViews();
  notice(
    `已按数据集 ${name} 套用导出图像：${detail.video_keys.join("、")}` +
      (missing.length ? `（无法识别：${missing.join("、")}）` : ""),
    missing.length > 0,
  );
}
function updateSelectionCount() {
  const unexported = stoppedRecords().filter((r) => !r.exported_to.length).length;
  $("selectionCount").textContent = `已勾选 ${exportSelection.size} 条 · 尚未导出 ${unexported} 条`;
}
$("selectUnexported").onclick = () => {
  exportSelection = new Set(
    stoppedRecords().filter((r) => !r.exported_to.length && r.annotated).map((r) => r.name),
  );
  renderQualitySessions();
  const skipped = stoppedRecords().filter((r) => !r.exported_to.length && !r.annotated).length;
  if (skipped) notice(`已勾选尚未导出且已标注的录制；另有 ${skipped} 条尚未标注，未勾选`);
};
$("selectAllSessions").onclick = () => {
  exportSelection = new Set(stoppedRecords().map((r) => r.name));
  renderQualitySessions();
};
$("clearSelection").onclick = () => {
  exportSelection.clear();
  renderQualitySessions();
};

// ---- 回放与标注: batch processing ----------------------------------------
async function pollRecomputeAll() {
  clearTimeout(recomputeAllTimer);
  let s;
  try {
    s = await api("/api/pose/recompute-all");
  } catch {
    return;
  }
  const job = s.job;
  $("recomputeAll").disabled = s.running;
  $("recomputeAllStatus").className = s.failed.length ? "muted error" : "muted";
  $("recomputeAllStatus").textContent = s.running
    ? `离线重算 ${s.done + 1} / ${s.total}：${s.current}` +
      (job ? (job.queued ? "（排队中）" : ` ${job.done} / ${job.total || "…"} 帧`) : "") +
      (status?.recording ? " · 录制中，已降速" : "")
    : s.total
      ? `离线重算完成 ${s.total - s.failed.length} / ${s.total} 条` +
        (s.skipped ? `，跳过 ${s.skipped} 条` : "") +
        (s.failed.length ? `；失败：${s.failed.map((f) => `${f.name}（${f.error}）`).join("；")}` : "")
      : "";
  if (s.running) recomputeAllTimer = setTimeout(pollRecomputeAll, 2000);
  else if (s.total) {
    await refresh().catch(() => {});
    if (selectedName && !dirty && !editorDirty) await reloadReview().catch(() => {});
  }
}
bind("recomputeAll", async () => {
  const s = await api("/api/pose/recompute-all", "POST", {
    only_missing: $("recomputeOnlyMissing").checked,
    fast: $("recomputeFast").checked,
    sessions: [...drawerSelection],
  });
  notice(
    s.total
      ? `开始依次离线重算 ${s.total} 条录制` + (s.skipped ? `（跳过 ${s.skipped} 条）` : "") +
          "；录制可照常进行，期间重算自动降速"
      : `没有需要离线重算的录制（跳过 ${s.skipped} 条）`,
  );
  pollRecomputeAll();
});
async function pollDraftAll() {
  clearTimeout(draftAllTimer);
  let s;
  try {
    s = await api("/api/drafts");
  } catch {
    return;
  }
  if (s.running) {
    const running = Object.entries(s.jobs).find(([, j]) => !j.queued);
    $("draftAllStatus").textContent =
      `预标注进行中，剩 ${s.running} 条` + (running ? `：${running[0]} ${running[1].done} / ${running[1].total || "…"} 段` : "");
    draftAllTimer = setTimeout(pollDraftAll, 3000);
  } else if ($("draftAllStatus").textContent) {
    $("draftAllStatus").textContent = "预标注全部完成，打开录制即可载入草稿核对";
    refresh().catch(() => {});
  }
}
bind("draftAll", async () => {
  const s = await api("/api/drafts/run-all", "POST", {
    only_missing: $("draftOnlyMissing").checked,
    sessions: [...drawerSelection],
  });
  $("draftAllStatus").textContent = s.queued.length ? `已排队 ${s.queued.length} 条` : "";
  notice(
    s.queued.length
      ? `已为 ${s.queued.length} 条录制排队预标注` + (s.skipped ? `（跳过 ${s.skipped} 条）` : "") + "，完成后打开录制即可载入草稿核对"
      : `没有需要预标注的录制（跳过 ${s.skipped} 条）`,
  );
  pollDraftAll();
});

// ---- 静止分段参数 ---------------------------------------------------------
async function loadSegParams(values) {
  const d = values || (await api("/api/segment-settings"));
  $("segParamFields").replaceChildren(
    ...d.fields.map((f) => {
      const label = el("label", undefined, "seg-param");
      const input = document.createElement("input");
      Object.assign(input, { type: "number", min: f.min, max: f.max, step: "any", value: f.value });
      input.dataset.key = f.key;
      label.title = `默认 ${f.default} ${f.unit}，范围 ${f.min}–${f.max}`;
      label.append(el("span", f.label), input, el("small", f.unit));
      return label;
    }),
  );
  const changed = d.fields.filter((f) => f.value !== f.default).length;
  $("segParamsSummary").textContent = changed ? `· 已修改 ${changed} 项` : "· 默认";
}
bind("saveSegParams", async () => {
  const values = {};
  for (const input of $("segParamFields").querySelectorAll("input"))
    values[input.dataset.key] = Number(input.value);
  await loadSegParams(await api("/api/segment-settings", "POST", values));
  notice("静止分段参数已保存，之后的“静止分段”和预标注都按新参数切分");
});
bind("resetSegParams", async () => {
  await loadSegParams(await api("/api/segment-settings", "POST", { reset: true }));
  notice("静止分段参数已恢复默认");
});
bind("batchExport", async () => {
  const target = $("datasetTarget").value;
  let name = target;
  if (target === "__new__") {
    name = $("newDatasetName").value.trim();
    if (!/^[A-Za-z0-9_-]+$/.test(name)) {
      $("exportResult").textContent =
        "数据集文件夹名只能包含字母、数字、下划线和短横线";
      return;
    }
  }
  const chosen = [...exportSelection];
  if (!chosen.length) return;
  exportBusy = true;
  updateBatchExport();
  $("exportResult").textContent = `正在导出 ${chosen.length} 条记录，请稍候…`;
  try {
    if (!exportCatalog) await loadExportCatalog();
    const views = selectedViews();
    if (!views.length) throw Error("请至少勾选一路导出图像");
    const r = await api(`/api/datasets/${encodeURIComponent(name)}/export`, "POST", {
      sessions: chosen,
      views,
      hand_crop_m: Number($("handCropM").value) || undefined,
    });
    $("exportResult").replaceChildren(
      el("strong", "导出完成"),
      el(
        "p",
        `累计 ${r.detail.episode_count} 个片段 · ${r.detail.total_frames} 帧，来自 ${r.detail.sessions.length} 条录制`,
      ),
      el("p", r.detail.name),
    );
    exportSelection.clear();
    await refresh();
    await refreshDatasets();
  } catch (e) {
    $("exportResult").textContent = e.message;
    throw e;
  } finally {
    exportBusy = false;
    updateBatchExport();
  }
});
document
  .querySelectorAll("[data-view]")
  .forEach((b) => (b.onclick = () => scene.view(b.dataset.view)));
$("zoomIn").onclick = () => scene.zoom(0.8);
$("zoomOut").onclick = () => scene.zoom(1.25);
$("trailMode").onchange = () => {
  trailMode = $("trailMode").checked;
  if (mode === "capture") scene.setData(recentRows(liveRows, 5));
};
$("clearTrail").onclick = () => {
  liveRows = [];
  scene.setData([]);
};
async function toggleFullscreen(node) {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await node.requestFullscreen();
  } catch {
    notice("浏览器暂不支持全屏查看", true);
  }
}
$("sceneFullscreen").onclick = () =>
  toggleFullscreen(document.querySelector(".scene-panel"));
document.addEventListener("fullscreenchange", () => {
  const sceneActive =
    document.fullscreenElement === document.querySelector(".scene-panel");
  $("sceneFullscreen").textContent = sceneActive ? "退出全屏" : "全屏";
  $("sceneFullscreen").setAttribute(
    "aria-label",
    sceneActive ? "退出三维轨迹全屏" : "全屏查看三维轨迹",
  );
  for (const card of $("videos").children) {
    const b = card.querySelector("[data-fullscreen]");
    const active = document.fullscreenElement === card;
    b.innerHTML = active ? "退出全屏" : FULLSCREEN_ICON;
    b.setAttribute(
      "aria-label",
      active ? "退出相机全屏" : "全屏查看" + title(card.dataset.stream),
    );
  }
});
window.addEventListener("hashchange", changeMode);
window.addEventListener("beforeunload", (e) => {
  if (dirty || editorDirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
// Del removes the picked segments (or the one open in the editor). Checked
// before the button guard below: a clicked segment leaves focus on its button.
function deletePickedParts() {
  const targets = selection.size ? selectedParts() : editing >= 0 ? [parts[editing]] : [];
  if (!targets.length) {
    notice("先点击要删除的片段（Ctrl / Shift 点击可多选），再按 Del", true);
    return;
  }
  const previous = parts.map((p) => ({ ...p })),
    numbers = targets.map((p) => parts.indexOf(p) + 1);
  parts = parts.filter((p) => !targets.includes(p));
  selection = new Set();
  anchor = null;
  setDirty(true);
  resetEditor();
  renderParts();
  notice(`已删除第 ${numbers.join("、")} 段，保存后生效。`);
  offerUndo("撤销删除", previous);
}
// Text entry, where Space and letters belong to the field.
function typing(target) {
  if (target.isContentEditable || target.tagName === "TEXTAREA") return true;
  return (
    target.tagName === "INPUT" &&
    !["range", "checkbox", "radio", "button", "submit", "file", "color"].includes(target.type)
  );
}
// A focused button activates on Space keyup; swallow that so the keydown
// handler's play / pause is not followed by a stray click.
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && (mode === "review" || (mode === "quality" && lerobot.loaded)) && !typing(e.target))
    e.preventDefault();
});
// 处理与导出: Space / ← → drive the LeRobot player once an episode is loaded.
document.addEventListener("keydown", (e) => {
  if (mode !== "quality" || !lerobot.loaded || typing(e.target) || e.target.tagName === "SELECT") return;
  if (e.target.id === "qualityResizer") return; // arrows resize the quality column there
  if ($("deleteDialog").open || $("lrBrowser").open || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.code === "Space") {
    e.preventDefault();
    if (e.repeat) return;
    if (e.target !== document.body) e.target.blur?.();
    lerobot.toggle();
  } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    if (e.target.type === "range") return; // the slider steps by itself
    e.preventDefault();
    lerobot.step(e.key === "ArrowLeft" ? -1 : 1);
  }
});
document.addEventListener("keydown", (e) => {
  if (
    e.key === "Delete" &&
    mode === "review" &&
    review &&
    !reviewBusy &&
    !["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) &&
    !e.target.isContentEditable &&
    !$("discardDialog").open
  ) {
    e.preventDefault();
    deletePickedParts();
    return;
  }
  if (mode !== "review" || !review || $("discardDialog").open) return;
  // Space plays / pauses even when a button, select or slider has focus (after
  // a click it usually does): there Space would otherwise press that control
  // instead. Only text fields keep Space for typing.
  if (e.code === "Space" && !typing(e.target)) {
    e.preventDefault();
    if (e.repeat) return;
    if (e.target !== document.body) e.target.blur?.();
    $("play").click();
    return;
  }
  // Shortcuts work over buttons and the timeline slider too, but not while
  // typing, inside a dropdown, or for Enter on a button (that presses it).
  if (
    typing(e.target) ||
    e.target.tagName === "SELECT" ||
    (e.key === "Enter" && e.target.tagName === "BUTTON")
  )
    return;
  const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
  if (e.key.toLowerCase() === "m" && plain) {
    e.preventDefault();
    mergeNeighbour(!e.shiftKey);
  } else if (e.key === "Enter" && plain) {
    e.preventDefault();
    confirmAndNext();
  } else if (/^[1-9]$/.test(e.key) && plain) {
    e.preventDefault();
    pickByNumber(Number(e.key));
  } else if ((e.key === "[" || e.key === "]") && plain) {
    e.preventDefault();
    edgeToPlayhead(e.key === "[");
  } else if (e.key.toLowerCase() === "b" && plain) {
    e.preventDefault();
    nearestBoundaryToPlayhead(e.shiftKey);
  } else if (e.key === "ArrowLeft") {
    e.preventDefault();
    $("prevFrame").click();
  } else if (e.key === "ArrowRight") {
    e.preventDefault();
    $("nextFrame").click();
  } else if (e.key.toLowerCase() === "i") $("markStart").click();
  else if (e.key.toLowerCase() === "o") $("markEnd").click();
});
armCards();
changeMode();
pollStatus();
pollLive();
renderReplay();
requestAnimationFrame(tick);
api("/api/action-table")
  .then((table) => {
    actionTable = table;
    renderActionTable();
  })
  .catch((e) => notice("动作表加载失败：" + e.message, true));
refresh()
  .then(() => {
    const session = new URL(location).searchParams.get("session");
    if (
      session &&
      records.some((r) => r.name === session && r.status === "stopped")
    ) {
      $("sessions").value = session;
      return selectSession(session);
    }
  })
  .catch((e) => notice(e.message, true));
loadSegParams().catch(() => {});
