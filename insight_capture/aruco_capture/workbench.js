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
};
let mode = "capture",
  status = null,
  records = [],
  selectedName = "",
  review = null,
  parts = [],
  dirty = false,
  editing = -1,
  playing = false,
  currentTime = 0,
  frameBusy = false,
  frameVersion = 0,
  liveRows = [],
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
  datasetDetailName = "";
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
  for (const id of ["prevFrame", "nextFrame", "qc", "save", "add", "autoSegment"])
    $(id).disabled = !review || reviewBusy;
  $("sessions").disabled = reviewBusy || exportBusy;
  for (const id of ["from", "to", "task", "hands"]) $(id).disabled = reviewBusy;
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
async function pollStatus() {
  try {
    const s = await api("/api/status");
    status = s;
    liveStreams = s.streams;
    $("connection").textContent = "服务已连接";
    $("connectionDot").className = "online";
    $("elapsed").textContent = seconds(s.elapsed_s, 1);
    $("recordingState").textContent = s.recording ? "正在录制" : "未录制";
    $("recordingState").className = "badge" + (s.recording ? " recording" : "");
    if (document.activeElement !== $("sampleRate")) $("sampleRate").value = s.fps;
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
        scene.setData(liveRows, lastPoseTime === 0);
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
    quality: "质量与导出",
  }[mode];
  $("contextHint").textContent = {
    capture: "检查视频、定位和开合度，准备一次完整采集。",
    review: "在同一时间轴检查画面与位姿，划分动作并填写任务。",
    quality: "定位无效时段，确认可用数据，再导出训练集。",
  }[mode];
  $("readiness").hidden = mode !== "capture";
  $("devicePanel").hidden = mode !== "capture";
  $("sourceAlerts").hidden =
    mode !== "capture" || !$("sourceAlerts").textContent;
  $("sessionBar").hidden = mode !== "review";
  $("stage").hidden = mode === "quality";
  $("armCards").hidden = mode === "quality";
  $("reviewPanel").hidden = mode !== "review";
  $("qualityPanel").hidden = mode !== "quality";
  $("cameraTitle").textContent = mode === "capture" ? "相机画面" : "同步视频";
  scene.setVisible(mode !== "quality");
  if (mode === "capture") {
    videoCards(liveStreams);
    scene.setData(liveRows, true);
    $("sceneEmpty").querySelector("strong").textContent = "等待双臂定位";
  } else {
    videoCards(review?.streams || liveStreams);
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
}
function stoppedRecords() {
  return records.filter((r) => r.status === "stopped");
}
function updateSessionNav() {
  const stopped = stoppedRecords();
  const index = stopped.findIndex((r) => r.name === selectedName);
  $("prevSession").disabled = index <= 0;
  $("nextSession").disabled = index === -1 || index >= stopped.length - 1;
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
  editorDirty = false;
  review = null;
  parts = [];
  setDirty(false);
  editing = -1;
  currentTime = 0;
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
  parts = data.segments.map((p) => ({ ...p, hands: p.hands || "both" }));
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
  if (mode !== "capture") scene.setData(data.rows, true);
  seek(Math.min(currentTime, data.duration_s));
  $("playbackNote").textContent =
    "TCP 与开合度随拖动即时更新；视频画面速率受浏览器解码限制，可能略有滞后";
  $("timelineMarks").replaceChildren(
    ...Array.from({ length: 6 }, (_, i) =>
      el("span", seconds((data.duration_s * i) / 5, 1)),
    ),
  );
}
function seek(t) {
  if (!review) return;
  currentTime = Math.max(0, Math.min(Number(t), review.duration_s));
  $("time").value = currentTime;
  $("playTime").textContent =
    `${seconds(currentTime)} / ${seconds(review.duration_s)}`;
  displayPose(currentTime);
}
function displayPose(t) {
  if (!review) return;
  const row =
    review.rows[Math.min(review.rows.length - 1, Math.round(t * review.fps))];
  scene.cursor(row);
  armCards(row?.arms, row?.grippers);
  $("frameLabel").textContent = `录制参考系 · ${seconds(t)} · 米`;
}
let replayAbort = null;
function renderReplay() {
  if (
    review &&
    mode === "review" &&
    !document.hidden &&
    lastRenderedFrame !== selectedName + ":" + currentTime
  ) {
    // A newer scrub position always wins: cancel whatever frames are still
    // in flight for the old one instead of waiting for them to finish, so
    // fast dragging chases the latest slider position, not a stale queue.
    replayAbort?.abort();
    replayAbort = new AbortController();
    const version = frameVersion,
      time = currentTime,
      signal = replayAbort.signal;
    lastRenderedFrame = selectedName + ":" + time;
    for (const card of $("videos").children)
      card.querySelector(".video-rate").textContent = seconds(time);
    for (const card of $("videos").children) {
      loadImage(
        card,
        base() + "/frame/" + encodeURIComponent(card.dataset.stream) + "?t=" + time,
        true,
        signal,
      ).then((result) => {
        if (version === frameVersion) applyImages([result], version);
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
}
function editPart(i) {
  const p = parts[i];
  editing = i;
  editorDirty = false;
  $("from").value = p.start_s;
  $("to").value = p.end_s;
  $("task").value = p.task;
  $("hands").value = p.hands || "both";
  $("editorTitle").textContent = "编辑动作片段";
  $("add").textContent = "更新片段";
  $("cancelEdit").hidden = false;
  seek(p.start_s);
}
function renderParts() {
  $("segments").replaceChildren();
  $("segmentTrack").replaceChildren();
  $("segmentCount").textContent = `${parts.length} 个片段`;
  if (!parts.length)
    $("segments").append(
      el("div", "拖动时间轴，设置起止点，添加第一个动作片段。", "empty-state"),
    );
  parts.forEach((p, i) => {
    const row = el("div", undefined, "segment-row"),
      b = el("button");
    b.type = "button";
    b.append(
      el("p", p.task),
      el(
        "small",
        `${seconds(p.start_s)} — ${seconds(p.end_s)} · ${title(p.hands || "both")}`,
      ),
    );
    b.onclick = () => editPart(i);
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
      const block = el("button", p.task, p.hands || "both");
      block.style.left = (p.start_s / review.duration_s) * 100 + "%";
      block.style.width =
        ((p.end_s - p.start_s) / review.duration_s) * 100 + "%";
      block.title = `${p.task}：${seconds(p.start_s)} 至 ${seconds(p.end_s)}`;
      block.onclick = () => editPart(i);
      $("segmentTrack").append(block);
    }
  });
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
          (r.exported_to.length
            ? ` · 已导出到：${r.exported_to.join(", ")}`
            : " · 尚未导出"),
      ),
    );
    row.append(check, info);
    row.onclick = () =>
      selectSession(r.name).catch((e) => notice(e.message, true));
    list.append(row);
  }
  updateBatchExport();
}
function updateBatchExport() {
  $("batchExport").disabled =
    exportSelection.size === 0 || exportBusy || !!status?.recording;
}
$("datasetTarget").onchange = () => {
  $("newDatasetName").hidden = $("datasetTarget").value !== "__new__";
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
$("sampleRate").addEventListener("change", async () => {
  const input = $("sampleRate"),
    fps = Number(input.value);
  try {
    await api("/api/settings", "POST", { fps });
    notice(`采样率已改为 ${fps} Hz，下次开始录制起生效`);
  } catch (e) {
    notice(e.message, true);
    if (status) input.value = status.fps;
  }
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
$("segmentForm").addEventListener("input", () => {
  editorDirty = true;
});
$("segmentForm").onsubmit = (e) => {
  e.preventDefault();
  if (!review || reviewBusy) return;
  const p = {
    start_s: Number($("from").value),
    end_s: Number($("to").value),
    task: $("task").value.trim(),
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
    $("from").focus();
    return;
  }
  if (editing >= 0) parts[editing] = p;
  else parts.push(p);
  parts.sort((a, b) => a.start_s - b.start_s);
  setDirty(true);
  resetEditor();
  renderParts();
};
bind("save", async () => {
  if (editorDirty) throw Error("请先添加或更新正在编辑的片段，再保存全部标注");
  reviewBusy = true;
  updateControls();
  try {
    await api(base() + "/segments", "POST", parts);
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
  if (editorDirty) throw Error("请先添加或更新正在编辑的片段，再自动分段");
  reviewBusy = true;
  updateControls();
  try {
    const suggestions = await api(base() + "/segments/auto");
    const previous = parts;
    parts = suggestions;
    setDirty(true);
    resetEditor();
    renderParts();
    notice(
      `已按双臂静止检测生成 ${suggestions.length} 个片段草稿，请逐段填写任务文本后保存；保存前可撤销。`,
    );
    const undo = el("button", "撤销自动分段", "quiet");
    undo.type = "button";
    undo.onclick = () => {
      parts = previous;
      setDirty(true);
      renderParts();
      notice("已撤销自动分段");
    };
    $("message").append(undo);
  } finally {
    reviewBusy = false;
    updateControls();
  }
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
    const r = await api(`/api/datasets/${encodeURIComponent(name)}/export`, "POST", {
      sessions: chosen,
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
