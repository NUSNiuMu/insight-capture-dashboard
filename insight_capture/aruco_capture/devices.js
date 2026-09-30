const $ = (id) => document.getElementById(id);
const make = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
function describeCalEntry(e) {
  if (e.error === "duplicate angle")
    return `错误：标定角度重复（${e.angle_degree?.toFixed(2)}°）`;
  if (e.error) return `错误：${e.error}`;
  if (e.info === "point added")
    return `第 ${e.index + 1} 点：角度 ${e.angle_degree?.toFixed(2)}° → 开合度 ${e.opening_mm?.toFixed(1)}mm`;
  if (e.info === "calibration saved") return `标定已保存，共 ${e.points} 点`;
  if (e.info === "calibration loaded from flash")
    return `已加载 flash 标定（${e.points} 点）`;
  if (e.info === "using default calibration")
    return `使用默认标定（${e.points} 点）`;
  return e.info || JSON.stringify(e);
}
export class DevicePanel {
  constructor(api) {
    this.api = api;
    this.data = null;
    this.status = null;
    this.busy = false;
    this.dirty = false;
    this.calibrating = {};
    this.calAfter = {};
    this.calEntries = {};
    this.calAngle = {};
    $("scanDevices").onclick = () => this.scan();
    $("devicePanel").addEventListener("toggle", () => {
      if ($("devicePanel").open && !this.data) this.scan();
    });
    $("deviceForm").addEventListener("input", () => {
      this.dirty = true;
      $("deviceMessage").textContent = "选择尚未应用";
    });
    $("deviceForm").onsubmit = async (e) => {
      e.preventDefault();
      this.busy = true;
      this.update(this.status);
      $("deviceMessage").textContent = "正在切换设备…";
      try {
        this.data = await this.api("/api/devices", "POST", this.bindings());
        this.dirty = false;
        this.render();
        $("deviceMessage").textContent =
          "设备选择已保存，正在连接；插入设备后会自动恢复。";
      } catch (error) {
        $("deviceMessage").textContent = error.message;
      } finally {
        this.busy = false;
        this.update(this.status);
      }
    };
  }
  async scan() {
    const draft = this.dirty && this.data ? this.bindings() : null;
    this.busy = true;
    this.update(this.status);
    $("deviceMessage").textContent = "正在扫描…";
    try {
      this.data = await this.api("/api/devices");
      if (draft) this.data.bindings = draft;
      this.render();
      const a = this.data.available;
      $("deviceMessage").textContent =
        `找到 ${a.video.length} 个视频端口、${a.serial.length} 个串口${draft ? "；保留未应用选择" : ""}`;
    } catch (e) {
      $("deviceMessage").textContent = e.message;
    } finally {
      this.busy = false;
      this.update(this.status);
    }
  }
  select(kind, current) {
    const select = make("select");
    select.dataset.kind = kind;
    const rows = this.data.available[kind];
    if (kind === "serial") select.add(new Option("暂不连接编码器", ""));
    for (const row of rows)
      select.add(
        new Option(
          `${row.label} · ${row.node}${row.stable ? " · 固定标识" : ""}${row.error ? " · 权限/查询异常" : ""}`,
          row.device,
        ),
      );
    const match = rows.find((r) => r.device === current || r.node === current);
    if (current && !match)
      select.add(new Option(`${current} · 当前未发现，等待重连`, current));
    select.value = match?.device || current || "";
    return select;
  }
  row(label, select, key) {
    const d = make("div", undefined, "device-row"),
      l = make("label", label);
    const id = "binding-" + key.replace("/", "-");
    select.id = id;
    l.htmlFor = id;
    d.append(l, select);
    return d;
  }
  render() {
    if (!this.data) return;
    $("deviceFields").replaceChildren();
    for (const item of this.data.bindings.rgb) {
      const select = this.select("video", item.device);
      select.dataset.name = item.name;
      const original = this.data.default_rgb.find((x) => x.name === item.name);
      const topic = item.topic || original?.topic;
      if (topic) {
        const option = new Option(`ROS · ${topic}`, "ros");
        option.dataset.topic = topic;
        select.insertBefore(option, select.firstChild);
        if (!item.device) select.value = "ros";
      }
      const d = this.row(
        item.name === "rgb_left"
          ? "左侧 RGB"
          : item.name === "rgb_right"
            ? "右侧 RGB"
            : item.name,
        select,
        item.name,
      );
      d.append(
        make("small", "优先固定设备标识；无标识的端口变号后，请重新扫描选择。"),
      );
      $("deviceFields").append(d);
    }
    for (let i = 0; i < 2; i++) {
      const item = this.data.bindings.serial[i] || {},
        select = this.select("serial", item.device);
      select.dataset.index = i;
      const d = this.row(`编码器串口 ${i + 1}`, select, "serial" + i),
        extra = make("div", undefined, "serial-options");
      const baud = make("input");
      baud.type = "number";
      baud.min = 300;
      baud.max = 4000000;
      baud.value = item.baudrate || 115200;
      baud.dataset.baud = i;
      baud.id = "baud" + i;
      const label = make("label", "波特率");
      label.htmlFor = baud.id;
      const hands = make("select");
      hands.dataset.hands = i;
      hands.id = "serialHands" + i;
      for (const [value, text] of [
        ["both", "双手（同一串口）"],
        ["left", "左手"],
        ["right", "右手"],
      ])
        hands.add(new Option(text, value));
      hands.value = item.hands || (i ? "right" : "both");
      const hl = make("label", "数据通道");
      hl.htmlFor = hands.id;
      extra.append(label, baud, hl, hands);
      d.append(extra);
      d.append(this.calibrationPanel(i));
      $("deviceFields").append(d);
    }
  }
  calibrationPanel(i) {
    const panel = make("div", undefined, "cal-panel"),
      title = make("strong", "夹爪标定"),
      start = make("button", "开始标定"),
      openingLabel = make("label", "开合度 (mm)"),
      opening = make("input"),
      record = make("button", "记录点"),
      restart = make("button", "重新标定"),
      angle = make("span", "—", "cal-angle"),
      log = make("div", undefined, "cal-log");
    opening.type = "number";
    opening.step = "10";
    start.type = "button";
    record.type = "button";
    restart.type = "button";
    start.onclick = () =>
      this.calibrating[i] ? this.endCalibration(i) : this.startCalibration(i);
    record.onclick = () => this.recordPoint(i, Number(opening.value));
    restart.onclick = () => this.restartCalibration(i);
    const row = make("div", undefined, "cal-row");
    row.append(openingLabel, opening, record, restart, angle);
    panel.append(title, start, row, log);
    panel.dataset.cal = i;
    this.renderCalibrationState(i, panel);
    return panel;
  }
  renderCalibrationState(i, panel) {
    panel ??= document.querySelector(`.cal-panel[data-cal="${i}"]`);
    if (!panel) return;
    const active = !!this.calibrating[i];
    const [start, row, log] = [
      panel.children[1],
      panel.children[2],
      panel.children[3],
    ];
    const locked = this.busy || !!this.status?.recording;
    start.disabled = locked;
    start.textContent = active ? "退出标定并保存" : "开始标定";
    for (const el of row.querySelectorAll("input,button"))
      el.disabled = !active || locked;
    row.querySelector(".cal-angle").textContent = this.calAngle[i]
      ? `当前角度 ${this.calAngle[i].toFixed(2)}°`
      : active
        ? "等待角度读数"
        : "";
    const entries = this.calEntries[i] || [];
    log.replaceChildren(
      ...entries
        .slice(-8)
        .map((e) =>
          make("div", describeCalEntry(e), e.error ? "cal-log-error" : "cal-log-info"),
        ),
    );
    log.scrollTop = log.scrollHeight;
  }
  async startCalibration(i) {
    this.calibrating[i] = true;
    this.calAfter[i] = 0;
    this.calEntries[i] = [];
    this.calAngle[i] = null;
    this.renderCalibrationState(i);
    try {
      await this.api(`/api/serial/${i}/command`, "POST", {
        command: "start_cal",
      });
      this.pollCalibration(i);
    } catch (e) {
      this.calibrating[i] = false;
      $("deviceMessage").textContent = e.message;
    }
    this.renderCalibrationState(i);
  }
  async restartCalibration(i) {
    this.calEntries[i] = [];
    this.calAngle[i] = null;
    try {
      await this.api(`/api/serial/${i}/command`, "POST", {
        command: "start_cal",
      });
    } catch (e) {
      $("deviceMessage").textContent = e.message;
    }
    this.renderCalibrationState(i);
  }
  async recordPoint(i, value) {
    try {
      await this.api(`/api/serial/${i}/command`, "POST", {
        command: "opening_mm",
        value,
      });
    } catch (e) {
      $("deviceMessage").textContent = e.message;
    }
  }
  async endCalibration(i) {
    try {
      await this.api(`/api/serial/${i}/command`, "POST", {
        command: "end_cal",
      });
    } catch (e) {
      $("deviceMessage").textContent = e.message;
    }
    // Stays in calibrating state until the log confirms a save (or a
    // rejection, e.g. too few points) — the firmware keeps calMode on
    // failure and silently continues using the previous calibration.
    this.renderCalibrationState(i);
  }
  async pollCalibration(i) {
    if (!this.calibrating[i]) return;
    try {
      const r = await this.api(
        `/api/serial/${i}/log?after=${this.calAfter[i] || 0}`,
      );
      if (r.entries.length) {
        this.calEntries[i] = [...(this.calEntries[i] || []), ...r.entries];
        this.calAfter[i] = r.entries[r.entries.length - 1].seq;
        if (r.entries.some((e) => e.info === "calibration saved"))
          this.calibrating[i] = false;
      }
      if (r.angle) this.calAngle[i] = r.angle.angle_degree;
      this.renderCalibrationState(i);
    } catch {
      // Transient poll failures are retried on the next tick.
    }
    if (this.calibrating[i]) setTimeout(() => this.pollCalibration(i), 150);
  }
  bindings() {
    const result = { rgb: [], serial: [] };
    for (const item of this.data.bindings.rgb) {
      const select = [
          ...$("deviceFields").querySelectorAll("select[data-name]"),
        ].find((s) => s.dataset.name === item.name),
        next = { ...item };
      if (select.value === "ros") {
        next.topic = select.selectedOptions[0].dataset.topic;
        delete next.device;
      } else {
        next.device = select.value;
        delete next.topic;
        delete next.compressed;
      }
      result.rgb.push(next);
    }
    for (const select of $("deviceFields").querySelectorAll(
      "select[data-index]",
    )) {
      if (!select.value) continue;
      const i = Number(select.dataset.index);
      result.serial.push({
        ...this.data.bindings.serial[i],
        device: select.value,
        baudrate: Number($("baud" + i).value),
        hands: $("serialHands" + i).value,
      });
    }
    return result;
  }
  update(status) {
    this.status = status;
    $("scanDevices").disabled = this.busy || !status;
    $("applyDevices").disabled =
      this.busy ||
      !status ||
      !!status.recording ||
      status.reconfiguring ||
      !this.data;
    for (const e of $("deviceFields").querySelectorAll("input,select"))
      e.disabled = this.busy || !!status?.recording || !status;
    this.renderCalibrationState(0);
    this.renderCalibrationState(1);
    $("deviceHint").textContent = status?.recording
      ? "录制中仍会自动重连；更换绑定请先停止录制。"
      : "自动重连已启用。更换设备绑定请先停止录制。";
    const faults = Object.entries(status?.source_states || {})
      .filter(([, s]) => s.state !== "ready")
      .map(
        ([key, s]) =>
          `${key.startsWith("serial/") ? "编码器串口 " + (Number(key.split("/")[1]) + 1) : key.replace("image/", "").replace("vio/head", "Insight9 VIO")}：${s.message}`,
      );
    const text = faults.join("；");
    if ($("sourceAlerts").textContent !== text)
      $("sourceAlerts").textContent = text;
    $("sourceAlerts").hidden =
      !text || document.body.dataset.mode !== "capture";
  }
}
