const $ = (id) => document.getElementById(id);
const make = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
export class DevicePanel {
  constructor(api) {
    this.api = api;
    this.data = null;
    this.status = null;
    this.busy = false;
    this.dirty = false;
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
      $("deviceFields").append(d);
    }
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
