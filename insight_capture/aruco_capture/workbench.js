import { Trajectory, poseFromState } from "./trajectory.js";
import { DevicePanel } from "./devices.js";
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
const scene = new Trajectory($("scene"), $("sceneEmpty"));
const devicePanel = new DevicePanel(api);
const el = (tag, text, cls) => {
  const x = document.createElement(tag);
  if (text !== undefined) x.textContent = text;
  if (cls) x.className = cls;
  return x;
};
function notice(text, error = false) {
  $("message").hidden = !text;
  $("message").textContent = text;
  $("message").className = error ? "error" : "";
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
  $("dirty").hidden = !value;
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
  const active = !!status?.recording;
  $("start").hidden = active;
  $("stop").hidden = !active;
  $("start").disabled =
    actionBusy || exportBusy || !status || status?.reconfiguring;
  $("stop").disabled = actionBusy;
  $("play").disabled = !review;
  for (const id of ["prevFrame", "nextFrame", "qc", "save", "add", "autoSegment", "splitPart"])
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
  $("poseTools").hidden = mode !== "review";
  $("trailToggle").hidden = mode !== "capture";
  $("clearTrail").hidden = mode !== "capture";
  $("stage").hidden = mode === "quality";
  $("armCards").hidden = mode === "quality";
  $("reviewPanel").hidden = mode !== "review";
  $("qualityPanel").hidden = mode !== "review";
  $("exportPanel").hidden = mode !== "quality";
  $("batchBar").hidden = mode !== "review";
  $("cameraTitle").textContent = mode === "capture" ? "相机画面" : "同步视频";
  scene.setVisible(mode !== "quality");
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
  $("drawerList").querySelector(".active")?.scrollIntoView({ block: "nearest" });
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
    loadDraft(name, true);
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
  const t = currentTime;
  installReview(await api(base() + "/review"));
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
function editPart(i) {
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
  seek(p.start_s);
  renderGuide();
  document
    .querySelector(`#segments .segment-row[data-index="${i}"]`)
    ?.scrollIntoView({ block: "nearest" });
}
function unfilled(p) {
  return !p?.task?.trim();
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
  $("task").focus();
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
    step = !total ? 1 : missing ? (editing >= 0 ? 3 : 2) : dirty ? 4 : 5;
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
  const flagged = parts.filter((p) => p.flags?.length).length;
  $("segmentCount").textContent =
    `${parts.length} 个片段` +
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
function renderQuality() {
  const q = review?.quality;
  $("quality").replaceChildren();
  $("qualitySummary").replaceChildren();
  $("dimensions").replaceChildren();
  $("validityTrack").replaceChildren();
  if (!q) {
    $("quality").append(
      el("div", "选择一条录制，查看质量报告。", "empty-state"),
    );
    return;
  }
  const metrics = [
    [
      "完整有效率",
      (q.valid_ratios.all_required * 100).toFixed(1) + "%",
      "全部必需维度同时有效",
    ],
    ["预期采样帧", q.expected_frames.toLocaleString(), "包含缺失时段"],
    ["录制时长", seconds(q.duration_s, 1), `${q.fps} Hz 统一采样`],
    [
      "可导出帧",
      q.export_plan.frames.toLocaleString(),
      `${q.export_plan.episodes} 个连续片段`,
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
  for (const [start, end] of q.missing_intervals.all_required) {
    const b = el("button");
    b.style.left = (start / q.duration_s) * 100 + "%";
    b.style.width =
      (Math.min(end - start, q.duration_s - start) / q.duration_s) * 100 + "%";
    b.title = `缺失 ${seconds(start)} 至 ${seconds(Math.min(end, q.duration_s))}`;
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
    row.append(check, info);
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
    const remove = el("button", "删除文件夹", "quiet");
    remove.type = "button";
    remove.onclick = () => deleteDatasetFolder(d.name);
    actions.append(view, remove);
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
    (p.task ? $("from") : $("task")).focus();
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
  const filling = editing >= 0 && unfilled(parts[editing]),
    p = commitEditor();
  if (!p || !filling || focusUnfilled(p.start_s)) return;
  if (!parts.some(unfilled))
    notice("全部片段都已填写任务，点「保存全部标注」后生效");
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
    $("task").focus();
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
// Picking an entry writes its canonical text, unless the box already holds
// wording of the annotator's own, which is kept for language variety.
function prefill(input, entry, canonical) {
  const text = input.value.trim();
  if (entry && (!text || canonical.includes(text))) input.value = entry.text;
}
$("taskId").onchange = () => {
  const task = tableTask($("taskId").value),
    allSubtasks = actionTable.tasks.flatMap((t) => t.subtasks);
  prefill($("task"), task, actionTable.tasks.map((t) => t.text));
  if (!task?.subtasks.some((x) => x.id === $("subtaskId").value)) {
    // The old subtask belonged to another task; drop its canonical text too.
    if (allSubtasks.some((x) => x.text === $("subtask").value.trim()))
      $("subtask").value = "";
    fillSubtaskOptions("");
  } else {
    fillSubtaskOptions();
    // Same subtask id, but this task may word it differently.
    prefill(
      $("subtask"),
      task.subtasks.find((x) => x.id === $("subtaskId").value),
      allSubtasks.map((x) => x.text),
    );
  }
  editorDirty = true;
};
$("subtaskId").onchange = () => {
  const task = tableTask($("taskId").value);
  prefill(
    $("subtask"),
    task?.subtasks.find((x) => x.id === $("subtaskId").value),
    actionTable.tasks.flatMap((t) => t.subtasks.map((x) => x.text)),
  );
  editorDirty = true;
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
    text = job.queued
      ? "预标注排队中…"
      : `预标注中 ${job.done} / ${job.total || "…"} 段`;
  else if (job?.error) text = "预标注失败：" + job.error;
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
  $("runDraft").textContent = draft ? "重新预标注" : "预标注";
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
  parts = draft.segments.map((s) => ({
    start_s: s.start_s,
    end_s: s.end_s,
    task: s.task || "",
    subtask: s.subtask || "",
    task_id: s.task_id || "",
    subtask_id: s.subtask_id || "",
    hands: s.hands || "both",
    note: s.description || "",
    flags: s.flags || [],
  }));
  selection = new Set();
  setDirty(true);
  resetEditor();
  renderParts();
  const flagged = parts.filter((p) => p.flags.length).length;
  notice(
    `${auto ? "已自动载入" : "已载入"}预标注草稿 ${parts.length} 段` +
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
      parts.map(({ note, flags, ...p }) => p),
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
  const previous = parts;
  parts = suggestions;
  setDirty(true);
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
    setDirty(true);
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
document.addEventListener("keydown", (e) => {
  if (
    mode !== "review" ||
    !review ||
    ["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(e.target.tagName) ||
    $("discardDialog").open
  )
    return;
  if (e.code === "Space") {
    e.preventDefault();
    $("play").click();
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
