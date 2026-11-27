// LeRobot dataset playback on the 处理与导出 page: pick a dataset folder (or
// a path on the server), pick an episode, and play its camera videos, the
// two-arm trajectory and the state curves on one clock — laid out like
// 回放与标注. Read-only: nothing here writes to the dataset.
import { Trajectory, poseFromState } from "./trajectory.js";

const $ = (id) => document.getElementById(id);
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
async function api(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(body.error || `HTTP ${response.status}`);
  return body;
}
function clock(t) {
  const n = Math.max(0, t || 0);
  return `${String(Math.floor(n / 60)).padStart(2, "0")}:${(n % 60).toFixed(2).padStart(5, "0")}`;
}
const CAMERA_NAMES = {
  head: "头部相机",
  rgb_left_up: "左腕 · 上",
  rgb_left_down: "左腕 · 下",
  rgb_right_up: "右腕 · 上",
  rgb_right_down: "右腕 · 下",
};
// Curves drawn for the 20-dim two-arm state; other datasets get their first dims.
const ARM_CURVES = [
  { index: 9, label: "左夹爪开合", color: "#83abff" },
  { index: 19, label: "右夹爪开合", color: "#ffb77d" },
  { index: 2, label: "左 z", color: "#69cde4", dash: [4, 3] },
  { index: 12, label: "右 z", color: "#f3c46b", dash: [4, 3] },
];
const PALETTE = ["#83abff", "#ffb77d", "#7cd8b2", "#ff9b9b", "#f3c46b", "#69cde4"];
const DRIFT_S = 0.08;

export class LerobotPlayer {
  constructor(notice) {
    this.notice = notice;
    this.dataset = null; // summary from /api/lerobot
    this.source = ""; // the path= query value
    this.episode = null; // /api/lerobot/episode reply
    this.frame = 0;
    this.playing = false;
    this.token = 0;
    this.scene = null;
    $("lrDataset").onchange = () => {
      $("lrPath").hidden = $("lrDataset").value !== "__path__";
      if (!$("lrPath").hidden) $("lrPath").focus({ preventScroll: true });
    };
    $("lrPath").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.openSelected();
    });
    $("lrOpen").onclick = () => this.openSelected();
    this.picker = new FolderPicker((path) => {
      $("lrDataset").value = "__path__";
      $("lrPath").hidden = false;
      $("lrPath").value = path;
      this.open(path);
    });
    $("lrBrowse").onclick = () => {
      const typed = $("lrDataset").value === "__path__" ? $("lrPath").value.trim() : "";
      this.picker.show(typed || this.dataset?.path || "");
    };
    $("lrFilter").oninput = () => this.renderEpisodes();
    $("lrPlay").onclick = () => this.toggle();
    $("lrPrev").onclick = () => this.step(-1);
    $("lrNext").onclick = () => this.step(1);
    $("lrPrevEpisode").onclick = () => this.neighbour(-1);
    $("lrNextEpisode").onclick = () => this.neighbour(1);
    $("lrSpeed").onchange = () => {
      for (const v of this.videos()) v.playbackRate = this.speed();
    };
    $("lrSeek").oninput = () => {
      const frame = Number($("lrSeek").value); // pause() moves the slider back
      this.pause();
      this.seek(frame);
    };
    new ResizeObserver(() => this.drawChart()).observe($("lrChart"));
  }
  get loaded() {
    return !!this.episode;
  }
  speed() {
    return Number($("lrSpeed").value) || 1;
  }
  videos() {
    return [...$("lrVideos").querySelectorAll("video")];
  }
  // Folders the page already lists under 数据集文件夹管理.
  setDatasets(list) {
    const select = $("lrDataset"),
      // "Other path" stays picked only once something was typed into it.
      previous = select.value === "__path__" && !$("lrPath").value.trim() ? "" : select.value;
    select.replaceChildren();
    for (const d of list) select.add(new Option(`${d.name}（${d.episode_count} 段）`, d.name));
    select.add(new Option("其他路径…", "__path__"));
    select.value = [...select.options].some((o) => o.value === previous)
      ? previous
      : select.options[0].value;
    $("lrPath").hidden = select.value !== "__path__";
  }
  openSelected() {
    const value =
      $("lrDataset").value === "__path__" ? $("lrPath").value.trim() : $("lrDataset").value;
    return this.open(value);
  }
  async open(source) {
    if (!source) {
      this.notice("请先选择数据集或填写路径", true);
      return;
    }
    if ([...$("lrDataset").options].some((o) => o.value === source)) $("lrDataset").value = source;
    $("lrPath").hidden = $("lrDataset").value !== "__path__";
    const token = ++this.token;
    $("lrSummary").textContent = "读取中…";
    let dataset;
    try {
      dataset = await api("/api/lerobot?path=" + encodeURIComponent(source));
    } catch (e) {
      $("lrSummary").textContent = "读取失败：" + e.message;
      return;
    }
    if (token !== this.token) return;
    this.pause();
    this.dataset = dataset;
    this.source = source;
    this.episode = null;
    $("lrSummary").textContent =
      `${dataset.name} · LeRobot ${dataset.version} · ${dataset.total_episodes} 个 episode · ` +
      `${dataset.total_frames} 帧 · ${dataset.fps} fps · ${dataset.video_keys.length} 路视频`;
    $("lrBody").hidden = false;
    if (!this.scene) this.scene = new Trajectory($("lrScene"), $("lrSceneEmpty"));
    this.scene.setVisible(true);
    this.renderEpisodes();
    if (dataset.episodes.length) await this.load(dataset.episodes[0].index);
    $("lerobotPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  episodeLabel(e) {
    return [e.subtask, ...(e.tasks || [])].filter(Boolean)[0] || "（无任务文本）";
  }
  filtered() {
    const q = $("lrFilter").value.trim().toLowerCase();
    const list = this.dataset?.episodes || [];
    if (!q) return list;
    return list.filter((e) =>
      [String(e.index), e.subtask, e.subtask_id, e.task_id, e.source_session, ...(e.tasks || [])]
        .filter(Boolean)
        .some((s) => String(s).toLowerCase().includes(q)),
    );
  }
  renderEpisodes() {
    const wrap = $("lrEpisodes");
    wrap.replaceChildren();
    const list = this.filtered(),
      fps = this.dataset?.fps || 30;
    $("lrEpisodeCount").textContent = this.dataset
      ? `${list.length} / ${this.dataset.episodes.length} 个 episode`
      : "";
    for (const e of list) {
      const row = el("button", undefined, "lr-episode");
      row.type = "button";
      row.setAttribute("role", "option");
      row.dataset.index = e.index;
      const selected = this.episode?.index === e.index;
      row.classList.toggle("selected", selected);
      row.setAttribute("aria-selected", selected);
      const head = el("div", undefined, "lr-episode-head");
      head.append(el("strong", `#${e.index}`), el("span", `${(e.length / fps).toFixed(1)} s · ${e.length} 帧`, "muted"));
      const text = el("div", this.episodeLabel(e), "lr-episode-text");
      row.append(head, text);
      if (e.subtask && e.tasks?.length) row.append(el("small", e.tasks[0], "muted"));
      if (e.source_session) row.append(el("small", e.source_session, "muted"));
      row.onclick = () => this.load(e.index);
      wrap.append(row);
    }
  }
  neighbour(delta) {
    const list = this.filtered();
    const at = list.findIndex((e) => e.index === this.episode?.index);
    const next = list[at + delta];
    if (next) return this.load(next.index, delta > 0 && this.playing);
  }
  async load(index, play = false) {
    const token = ++this.token;
    this.pause();
    let episode;
    try {
      episode = await api(
        `/api/lerobot/episode?path=${encodeURIComponent(this.source)}&index=${index}`,
      );
    } catch (e) {
      this.notice(`读取 episode ${index} 失败：${e.message}`, true);
      return;
    }
    if (token !== this.token) return;
    this.episode = episode;
    this.meta = this.dataset.episodes.find((e) => e.index === index) || {};
    this.frame = 0;
    $("lrSeek").max = Math.max(0, episode.length - 1);
    this.buildVideos();
    this.buildTrajectory();
    this.renderEpisodes();
    // Inside the list only: scrolling the page would yank the player away.
    const list = $("lrEpisodes"),
      row = list.querySelector(".selected");
    if (row) {
      const box = list.getBoundingClientRect(),
        r = row.getBoundingClientRect();
      if (r.top < box.top) list.scrollTop -= box.top - r.top;
      else if (r.bottom > box.bottom) list.scrollTop += r.bottom - box.bottom;
    }
    this.renderInfo();
    this.seek(0);
    if (play) this.play();
  }
  buildVideos() {
    const wrap = $("lrVideos");
    for (const v of this.videos()) v.removeAttribute("src");
    wrap.replaceChildren();
    // Head-camera views (head_view, subtask_end_head_view…) lead, side by side
    // in one row; a single head view takes the whole row. Wrist views follow.
    const isHead = ([key]) => /head/i.test(key.replace(/^observation\.images\./, ""));
    const all = Object.entries(this.episode.videos),
      entries = [...all.filter(isHead), ...all.filter((e) => !isHead(e))],
      heads = all.filter(isHead).length;
    wrap.classList.toggle("single", entries.length === 1);
    wrap.classList.toggle("pair", entries.length === 2);
    wrap.classList.toggle("lead", heads === 1 && entries.length > 2);
    this.framed = [];
    const playable = new Set(this.dataset.browser_codecs || []);
    for (const [key, info] of entries) {
      const name = key.replace(/^observation\.images\./, "");
      const card = el("div", undefined, "video-card");
      const box = el("div", undefined, "image-wrap");
      const label = el("div", undefined, "video-label");
      const rate = el("span", name, "video-rate");
      label.append(el("strong", CAMERA_NAMES[name] || name), rate);
      const codec = String(this.dataset.codecs?.[key] || "").toLowerCase();
      // A codec <video> cannot play (MPEG-4 Part 2 in many v2.x datasets) is
      // decoded on the server and shown frame by frame instead.
      const toFrames = () => {
        box.replaceChildren();
        const img = el("img");
        img.alt = name;
        box.append(img);
        rate.textContent = `${name} · ${codec || "未知编码"} · 逐帧解码`;
        const item = { img, info, loading: false, want: -1, shown: -1 };
        this.framed.push(item);
        this.requestFrame(item, this.frame);
      };
      if (codec && !playable.has(codec)) toFrames();
      else {
        const video = el("video");
        video.muted = true;
        video.playsInline = true;
        video.disablePictureInPicture = true; // its overlay button sat on the picture
        video.preload = "auto";
        video.dataset.from = info.from;
        video.dataset.to = info.to;
        video.src =
          `/api/lerobot/video?path=${encodeURIComponent(this.source)}&file=${encodeURIComponent(info.file)}`;
        video.addEventListener("loadedmetadata", () => this.place(video, this.frame), { once: true });
        video.onerror = () => {
          video.removeAttribute("src");
          toFrames();
        };
        box.append(video);
      }
      card.append(box, label);
      wrap.append(card);
    }
    if (!entries.length) wrap.append(el("div", "这个 episode 没有视频文件", "empty-state"));
  }
  // One request in flight per frame-by-frame camera; when it lands, ask for
  // the frame the clock has reached by then (frames in between are skipped).
  requestFrame(item, i) {
    item.want = i;
    if (item.loading || item.shown === i) return;
    item.loading = true;
    const target = i,
      t = item.info.from + target / this.episode.fps;
    const done = () => {
      item.loading = false;
      item.shown = target;
      if (item.want !== target && this.framed?.includes(item)) this.requestFrame(item, item.want);
    };
    item.img.onload = done;
    item.img.onerror = done;
    item.img.src =
      `/api/lerobot/frame?path=${encodeURIComponent(this.source)}` +
      `&file=${encodeURIComponent(item.info.file)}&t=${t.toFixed(4)}`;
  }
  // The 20-dim two-arm state of this system's exports (left 0–9, right 10–19).
  armState() {
    const names = this.dataset.names?.["observation.state"];
    return Array.isArray(names) && names.length === 20 && String(names[0]).startsWith("left");
  }
  buildTrajectory() {
    const rows = this.episode.rows;
    this.trajectory = this.armState()
      ? rows.map((r) => ({
          t: r.t,
          arms: {
            left: poseFromState(r["observation.state"], 0, r.valid),
            right: poseFromState(r["observation.state"], 10, r.valid),
          },
        }))
      : [];
    // No two-arm trajectory to draw: give the videos the whole width.
    document.querySelector(".lr-stage").classList.toggle("no-scene", !this.trajectory.length);
    this.scene.setVisible(this.trajectory.length > 0);
    this.scene.setCameraView(null);
    this.scene.setData(this.trajectory, true);
    this.scene.view("free");
    const names = (this.dataset.names?.["observation.state"] || []).map(String);
    // Other layouts: grippers and heights tell the most at a glance.
    let picked = names
      .map((label, index) => ({ label, index }))
      .filter(({ label }) => /gripper/i.test(label) || /(^|[._])z$/i.test(label))
      .slice(0, 4);
    if (!picked.length) picked = names.slice(0, 6).map((label, index) => ({ label, index }));
    this.curves = this.armState()
      ? ARM_CURVES
      : picked.map((c, k) => ({ ...c, color: PALETTE[k % PALETTE.length] }));
    if (!this.curves.length && rows[0]?.["observation.state"])
      this.curves = rows[0]["observation.state"]
        .slice(0, 6)
        .map((_, i) => ({ index: i, label: `state[${i}]`, color: PALETTE[i % PALETTE.length] }));
    $("lrLegend").replaceChildren(
      ...this.curves.map((c) => {
        const item = el("span", c.label);
        item.style.setProperty("--swatch", c.color);
        return item;
      }),
    );
    this.drawChart();
  }
  renderInfo() {
    const e = this.meta,
      fps = this.episode.fps;
    const lines = [
      ["episode", `#${this.episode.index} · ${this.episode.length} 帧 · ${(this.episode.length / fps).toFixed(2)} s`],
      ["任务", (e.tasks || []).join(" / ") || "—"],
    ];
    if (e.subtask) lines.push(["子任务", e.subtask + (e.subtask_id ? `（${e.subtask_id}）` : "")]);
    if (e.participating_hands) lines.push(["参与手臂", { left: "左手", right: "右手", both: "双手" }[e.participating_hands] || e.participating_hands]);
    if (e.source_session)
      lines.push([
        "来源录制",
        e.source_session +
          (e.source_start_s != null ? ` · ${clock(e.source_start_s)} – ${clock(e.source_end_s)}` : ""),
      ]);
    const box = $("lrEpisodeInfo");
    box.replaceChildren();
    for (const [k, v] of lines) box.append(el("span", k, "muted"), el("span", v));
  }
  // Frame `i` of the episode in each video's own file time.
  place(video, i) {
    const t = Number(video.dataset.from) + i / this.episode.fps;
    if (Math.abs(video.currentTime - t) > 1e-3) video.currentTime = t + 1e-4;
  }
  seek(i) {
    if (!this.episode) return;
    this.frame = Math.max(0, Math.min(this.episode.length - 1, Math.round(i)));
    for (const v of this.videos()) if (v.readyState >= 1) this.place(v, this.frame);
    this.renderFrame();
  }
  step(delta) {
    this.pause();
    this.seek(this.frame + delta);
  }
  renderFrame() {
    const ep = this.episode,
      row = ep.rows[this.frame];
    $("lrSeek").value = this.frame;
    for (const item of this.framed || []) this.requestFrame(item, this.frame);
    $("lrTime").textContent =
      `${clock(this.frame / ep.fps)} / ${clock((ep.length - 1) / ep.fps)} · 第 ${this.frame + 1} / ${ep.length} 帧`;
    this.scene?.cursor(this.trajectory?.[this.frame] || null);
    this.renderState(row);
    this.drawChart();
  }
  renderState(row) {
    const table = $("lrState");
    table.replaceChildren();
    if (!row) return;
    const state = row["observation.state"],
      action = row.action;
    if (!state) return;
    const header = el("tr");
    header.append(el("th", ""), el("th", "状态"), el("th", "动作（下一帧目标）"));
    table.append(header);
    const fmt = (v) => (v == null ? "—" : Number(v).toFixed(3));
    if (this.armState()) {
      for (const [side, o, label] of [
        ["left", 0, "左"],
        ["right", 10, "右"],
      ]) {
        const add = (name, pick) => {
          const tr = el("tr");
          tr.className = side;
          tr.append(el("th", label + name), el("td", pick(state)), el("td", action ? pick(action) : "—"));
          table.append(tr);
        };
        add(" 位置 m", (v) => v.slice(o, o + 3).map(fmt).join(", "));
        add(" 夹爪 m", (v) => fmt(v[o + 9]));
      }
    } else {
      const names = this.dataset.names?.["observation.state"] || [];
      state.slice(0, 12).forEach((v, i) => {
        const tr = el("tr");
        tr.append(el("th", String(names[i] ?? `state[${i}]`)), el("td", fmt(v)), el("td", fmt(action?.[i])));
        table.append(tr);
      });
    }
    if (!row.valid) table.append(el("caption", "此帧标为无效"));
  }
  drawChart() {
    const canvas = $("lrChart");
    const width = canvas.clientWidth,
      height = canvas.clientHeight;
    if (!width || !height) return;
    const ratio = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
    if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);
    const g = canvas.getContext("2d");
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.clearRect(0, 0, width, height);
    const rows = this.episode?.rows || [];
    if (rows.length < 2 || !this.curves?.length) return;
    const pad = 8,
      x = (i) => pad + (i / (rows.length - 1)) * (width - 2 * pad);
    g.strokeStyle = "#30425b";
    g.lineWidth = 1;
    g.strokeRect(0.5, 0.5, width - 1, height - 1);
    // Each curve on its own scale: the shape over time is what matters here.
    for (const c of this.curves) {
      const values = rows.map((r) => r["observation.state"]?.[c.index]).filter((v) => v != null);
      if (!values.length) continue;
      let lo = Math.min(...values),
        hi = Math.max(...values);
      if (hi - lo < 1e-6) {
        lo -= 0.5;
        hi += 0.5;
      }
      const y = (v) => height - pad - ((v - lo) / (hi - lo)) * (height - 2 * pad);
      g.beginPath();
      g.setLineDash(c.dash || []);
      g.strokeStyle = c.color;
      g.lineWidth = 1.5;
      let started = false;
      rows.forEach((r, i) => {
        const v = r["observation.state"]?.[c.index];
        if (v == null || !r.valid) {
          started = false;
          return;
        }
        if (started) g.lineTo(x(i), y(v));
        else g.moveTo(x(i), y(v));
        started = true;
      });
      g.stroke();
    }
    g.setLineDash([]);
    g.strokeStyle = "#e3ebf5";
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(x(this.frame) + 0.5, 0);
    g.lineTo(x(this.frame) + 0.5, height);
    g.stroke();
  }
  toggle() {
    if (this.playing) this.pause();
    else this.play();
  }
  play() {
    if (!this.episode) return;
    if (this.frame >= this.episode.length - 1) this.seek(0);
    this.playing = true;
    $("lrPlay").textContent = "❚❚ 暂停";
    const started = performance.now(),
      from = this.frame,
      fps = this.episode.fps,
      run = this.token;
    for (const v of this.videos()) {
      v.playbackRate = this.speed();
      v.play().catch(() => {});
    }
    const tick = () => {
      if (!this.playing || run !== this.token) return;
      const frame = from + ((performance.now() - started) / 1000) * fps * this.speed();
      if (frame >= this.episode.length - 1) {
        this.seek(this.episode.length - 1);
        this.pause();
        if ($("lrAutoNext").checked) this.neighbour(1)?.then(() => this.play());
        return;
      }
      this.frame = Math.floor(frame);
      // The page clock leads; a video that drifts is pulled back to it.
      const t = frame / fps;
      for (const v of this.videos()) {
        const want = Number(v.dataset.from) + t;
        if (v.readyState >= 2 && Math.abs(v.currentTime - want) > DRIFT_S) v.currentTime = want;
      }
      this.renderFrame();
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }
  pause() {
    this.playing = false;
    cancelAnimationFrame(this.raf);
    $("lrPlay").textContent = "▶ 播放";
    for (const v of this.videos()) v.pause();
    if (this.episode) this.seek(this.frame);
  }
  // 处理与导出 shown / hidden.
  setVisible(value) {
    if (!value) this.pause();
    this.scene?.setVisible(value && !!this.dataset);
  }
}

// Server-side folder picker for 其他路径: the browser's own file dialog only
// sees the viewer's computer, and the datasets live on the capture server.
class FolderPicker {
  constructor(onOpen) {
    this.onOpen = onOpen;
    this.current = null;
    this.picked = "";
    $("lrBrowserClose").onclick = $("lrBrowserCancel").onclick = () => $("lrBrowser").close();
    $("lrBrowserUp").onclick = () => this.current && this.go(this.current.parent ?? "");
    $("lrBrowserRoots").onclick = () => this.go("");
    $("lrBrowserOpen").onclick = () => this.finish(this.picked);
    $("lrBrowserPath").addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      this.go($("lrBrowserPath").value.trim());
    });
  }
  async show(start) {
    $("lrBrowser").showModal();
    // A typed path that is not a folder (or is outside the roots) falls back to the roots.
    if (!(await this.go(start, true))) await this.go("");
  }
  async go(path, quiet = false) {
    let listing;
    try {
      listing = await api("/api/lerobot/browse?path=" + encodeURIComponent(path));
    } catch (e) {
      if (!quiet) $("lrBrowserPick").textContent = e.message;
      return false;
    }
    this.current = listing;
    $("lrBrowserPath").value = listing.path;
    $("lrBrowserUp").disabled = listing.parent === null;
    this.pick(listing.dataset ? listing.path : "", listing.dataset);
    const list = $("lrBrowserList");
    list.replaceChildren();
    list.scrollTop = 0;
    if (!listing.entries.length) list.append(el("div", "这个文件夹下没有子文件夹", "empty-state"));
    for (const entry of listing.entries) {
      const row = el("button", undefined, "folder-row" + (entry.dataset ? " dataset" : ""));
      row.type = "button";
      row.setAttribute("role", "option");
      row.append(el("span", entry.dataset ? "▣" : "▸", "folder-icon"), el("span", entry.name, "folder-name"));
      if (entry.dataset)
        row.append(
          el(
            "span",
            `LeRobot ${entry.dataset.version}` + (entry.dataset.episodes != null ? ` · ${entry.dataset.episodes} 个 episode` : ""),
            "folder-badge",
          ),
        );
      row.onclick = () => {
        for (const r of list.querySelectorAll(".folder-row.selected")) r.classList.remove("selected");
        row.classList.add("selected");
        this.pick(entry.dataset ? entry.path : "", entry.dataset, entry.path);
      };
      row.ondblclick = () => (entry.dataset ? this.finish(entry.path) : this.go(entry.path));
      list.append(row);
    }
    if (listing.truncated) list.append(el("div", "文件夹太多，只列出前 1000 个；可在上方直接输入路径", "muted"));
    return true;
  }
  pick(path, dataset, folder = "") {
    this.picked = path;
    $("lrBrowserOpen").disabled = !path;
    $("lrBrowserPick").textContent = path
      ? `已选：${path}（LeRobot ${dataset.version}）`
      : folder
        ? "不是 LeRobot 数据集（缺少 meta/info.json），双击进入"
        : "选择一个数据集文件夹（带 ▣ 标记）";
  }
  finish(path) {
    if (!path) return;
    $("lrBrowser").close();
    this.onOpen(path);
  }
}
