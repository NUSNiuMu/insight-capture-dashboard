import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    property var selected: []
    property var known: []
    property var status: workspace.data.recording || {}
    property var catalog: workspace.data.topics || {}
    property var directories: workspace.data.directories || {}
    property var distributed: status.distributed || {}
    property bool finalizing: status.merge_state === "merging" || status.merge_state === "finalizing"
    property bool locked: !!status.recording || finalizing || !!workspace.busy.action || !!dashboard.state.captureAction
    property bool taskActive: !!(status.task_status || {}).active
    property bool starting: false
    heading: "Recorder · 录制"
    description: distributed.enabled ? "Ego 四路视频录到头环 TF 卡；Jetson 保存腕部数据。停止后校验取回并按原始采集时间戳合并。" : "选择 ROS 话题和存储目录，查看录制与保存状态。完整性检查请前往评分或录制库。"
    function browse(path) {
        workspace.request("directories", "/api/recording/storage/directories" + (path ? "?path=" + encodeURIComponent(path) : ""));
    }
    function toggle(name, checked) {
        var a = selected.slice(), i = a.indexOf(name);
        if (checked && i < 0)
            a.push(name);
        if (!checked && i >= 0)
            a.splice(i, 1);
        selected = a;
    }
    function syncTopics() {
        var topics = catalog.topics || [], previous = selected.slice(), old = known.slice();
        selected = old.length ? topics.filter(function (t) {
                return previous.indexOf(t) >= 0 || old.indexOf(t) < 0;
            }) : (catalog.default_selected_topics || []).filter(function (t) {
                return topics.indexOf(t) >= 0;
            });
        known = topics.slice();
    }
    Ui.Section {
        eyebrow: "01  CAPTURE CONTROL"
        heading: "录制控制"
        RowLayout {
            Layout.fillWidth: true
            Ui.ComboBox {
                id: captureTask
                objectName: "recording-task"
                Layout.fillWidth: true
                model: dashboard.tasks
                textRole: "name"
                valueRole: "task_id"
                enabled: !page.locked
            }
            Ui.Button {
                objectName: "recording-enter-task"
                text: "进入任务"
                highlighted: !page.taskActive
                enabled: !page.locked && captureTask.count > 0 && !dashboard.playback
                onClicked: shell.mutate("/api/tasks/" + encodeURIComponent(captureTask.currentValue) + "/activate")
            }
        }
        Label {
            text: page.taskActive ? "当前任务：" + (((page.status.task_status || {}).task || {}).name || "") : "当前未进入采集任务，请先选择任务并点击“进入任务”。"
            color: page.taskActive ? Ui.Theme.secondary : Ui.Theme.red
            Layout.fillWidth: true
            wrapMode: Text.Wrap
        }
        RowLayout {
            Layout.fillWidth: true
            Ui.TextField {
                id: name
                objectName: "recording-name"
                Layout.fillWidth: true
                placeholderText: "可选录制名称"
                enabled: !page.locked
            }
            Ui.Button {
                objectName: "recording-start"
                text: page.starting ? "正在起录…" : !page.taskActive ? "先进入任务" : "开始录制"
                highlighted: true
                enabled: !page.locked && page.taskActive && page.selected.length > 0 && !dashboard.playback
                onClicked: {
                    var body = {
                        "topics": page.selected,
                        "native_relaxed_preflight": true
                    };
                    if (name.text.trim())
                        body.bag_name = name.text.trim();
                    page.starting = true;
                    shell.mutate("/api/recording/start", body);
                }
            }
            Ui.Button {
                objectName: "recording-stop"
                tone: "danger"
                text: "停止录制"
                enabled: !!page.status.recording && !workspace.busy.action && !dashboard.state.captureAction
                onClicked: shell.mutate("/api/recording/stop")
            }
            Ui.Button {
                text: "刷新"
                onClicked: shell.refresh()
            }
        }
    }
    Ui.Section {
        visible: !!page.distributed.enabled
        heading: "双端录制与时间同步"
        Label {
            text: "状态：" + ({idle: "待命", starting: "正在启动两端", recording: "两端录制中", stop_pending: "等待两端保存确认", saved: "两端已保存", copying: "正在校验取回 Ego 数据", merging: "正在按采集时间合并", done: "六路已合并", error: "需要检查"}[page.distributed.phase] || page.distributed.phase || "待命")
            Layout.fillWidth: true
            wrapMode: Text.Wrap
        }
        Label {
            text: "共同录制编号：" + (page.distributed.session_id || "起录时生成")
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.muted
        }
        Label {
            text: "Ego TF 卡：" + (page.distributed.ego_path || "/userdata/sdcard/capture") + (page.distributed.remote_saved ? " · 已保存" : "")
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.muted
        }
        Label {
            text: "Jetson 腕部原件：" + (page.distributed.jetson_path || "起录时生成") + (page.distributed.local_saved ? " · 已保存" : "")
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.muted
        }
        Repeater {
            model: Object.keys((page.distributed.clock || {}).devices || {})
            delegate: Label {
                required property string modelData
                property var clock: ((page.distributed.clock || {}).devices || {})[modelData] || {}
                text: modelData + " · " + (clock.offset_ms !== undefined ? "时钟偏差 " + Number(clock.offset_ms).toFixed(2) + " ms · 偏差界限 " + Number(clock.offset_bound_ms).toFixed(2) + " ms" : clock.error || "未测量")
                color: clock.ok ? Ui.Theme.secondary : Ui.Theme.red
                Layout.fillWidth: true
                wrapMode: Text.Wrap
            }
        }
        Label {
            text: "起录门槛：单台及设备间偏差界限 ≤ " + ((page.distributed.clock || {}).max_clock_bound_ms || 10) + " ms；录制中持续检查。NTP 校时不代表硬件曝光同步。"
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: Ui.Theme.muted
        }
        Label {
            text: (page.distributed.clock || {}).checked_at_epoch_s ? "最近检查：" + new Date(page.distributed.clock.checked_at_epoch_s * 1000).toLocaleTimeString() : "开始录制前自动检查时钟"
            color: Ui.Theme.muted
            Layout.fillWidth: true
        }
        Label {
            text: page.distributed.error || (page.distributed.clock_violations || []).length ? (page.distributed.error || "本次时钟检查存在异常，需复核时间报告") : (page.distributed.quality_issues || []).length ? "本次视频连续性检查存在异常，需复核质量报告" : ""
            visible: text.length > 0
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: Ui.Theme.red
        }
        Ui.Button {
            visible: !!page.distributed.retry_merge
            text: "重试取回与合并"
            enabled: !workspace.busy.action
            onClicked: shell.mutate("/api/recording/merge/retry")
        }
    }
    Ui.Section {
        heading: "录制状态"
        RowLayout {
            Layout.fillWidth: true
            Ui.Badge {
                text: page.status.recording ? "录制中" : page.finalizing ? "正在保存" : page.status.merge_state === "error" ? "保存失败" : "待命"
                tone: page.status.recording ? "success" : page.status.merge_state === "error" ? "danger" : page.finalizing ? "accent" : "neutral"
            }
            Label {
                text: page.status.recording ? (page.distributed.enabled ? "Ego TF 卡与 Jetson 正在独立保存" : "正在采集所选话题") : page.finalizing ? "正在保存和合并录制，请等待完成…" : page.status.merge_state === "done" ? "录制已保存" + ((page.status.merge_timings || {}).total_sec !== undefined ? " · 耗时 " + Number(page.status.merge_timings.total_sec).toFixed(1) + " 秒" : "") : "准备开始录制"
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: Ui.Theme.secondary
                font.pixelSize: 14
            }
        }
        Label {
            text: page.status.output_path || ""
            visible: text.length > 0
            textFormat: Text.PlainText
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.muted
            font.pixelSize: 13
        }
        Label {
            text: page.status.merge_error || ""
            visible: text.length > 0
            textFormat: Text.PlainText
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.red
        }
        Label {
            property var taskStatus: page.status.task_status || {}
            property var stats: taskStatus.stats || {}
            text: taskStatus.active ? ((taskStatus.task || {}).name || "当前任务") + " · 已录制 " + (stats.recorded_takes || 0) + " 条 · 下一条 " + (stats.next_take_id || 1) : "暂无活动任务"
            Layout.fillWidth: true
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: Ui.Theme.muted
            font.pixelSize: 13
        }
    }
    Ui.Section {
        eyebrow: "02  RECORDING STORAGE"
        heading: "存储位置"
        Layout.fillWidth: true
        ColumnLayout {
            Layout.fillWidth: true
            Label {
                text: "录制存储目录"
                font.bold: true
            }
            Label {
                text: (page.status.storage || {}).active_path || "正在读取…"
                Layout.fillWidth: true
                wrapMode: Text.WrapAnywhere
                color: Ui.Theme.ink
                font.pixelSize: 14
            }
            Ui.Badge {
                property var space: page.status.disk_space || {}
                text: space.free_bytes !== undefined ? "可用 " + (Number(space.free_bytes) / (1024 * 1024 * 1024)).toFixed(1) + " GB · " + (Number(space.free_ratio) * 100).toFixed(0) + "%" : "可用空间未知"
                tone: space.free_ratio < 0.1 ? "danger" : space.free_ratio < 0.3 ? "accent" : "neutral"
            }
            Label {
                text: "正在浏览：" + (page.directories.path || "正在读取…")
                Layout.fillWidth: true
                wrapMode: Text.WrapAnywhere
                color: Ui.Theme.muted
            }
            RowLayout {
                Ui.Button {
                    text: "上一级"
                    enabled: !!page.directories.parent && !page.locked
                    onClicked: page.browse(page.directories.parent)
                }
                Ui.Button {
                    objectName: "storage-select"
                    text: "使用当前目录"
                    enabled: !!page.directories.selectable && !page.locked
                    onClicked: shell.mutate("/api/recording/storage/select", {
                            "path": page.directories.path
                        })
                }
            }
            Flow {
                Layout.fillWidth: true
                spacing: 8
                Repeater {
                    model: page.directories.roots || []
                    delegate: Ui.Button {
                        required property var modelData
                        text: modelData.name
                        enabled: !!modelData.available && !page.locked
                        onClicked: page.browse(modelData.path)
                    }
                }
            }
            Repeater {
                model: page.directories.directories || []
                delegate: Ui.Button {
                    required property var modelData
                    text: "▸ " + modelData.name
                    enabled: !!modelData.writable && !page.locked
                    onClicked: page.browse(modelData.path)
                }
            }
        }
    }
    RowLayout {
        Label {
            text: "录制话题 · 已选 " + page.selected.length + " / " + page.known.length
            font.bold: true
        }
        Ui.Button {
            text: "全选"
            enabled: !page.locked
            onClicked: page.selected = page.known.slice()
        }
        Ui.Button {
            text: "清空"
            enabled: !page.locked
            onClicked: page.selected = []
        }
        Ui.Button {
            text: "恢复默认"
            enabled: !page.locked
            onClicked: page.selected = (page.catalog.default_selected_topics || []).slice()
        }
    }
    Repeater {
        model: (page.catalog.cameras || []).concat((page.catalog.other || []).length ? [{
                    "label": "其他",
                    "topics": page.catalog.other
                }] : [])
        delegate: Ui.Panel {
            id: group
            required property var modelData
            Layout.fillWidth: true
            ColumnLayout {
                anchors.fill: parent
                Ui.CheckBox {
                    font.weight: Font.DemiBold
                    text: (group.modelData.label || group.modelData.namespace) + (page.distributed.enabled && group.modelData.namespace === "egohalo" ? "（TF 卡自动录制）" : "")
                    enabled: !page.locked && !(page.distributed.enabled && group.modelData.namespace === "egohalo")
                    checked: (group.modelData.topics || []).length > 0 && (group.modelData.topics || []).every(function (t) {
                            return page.selected.indexOf(t.name) >= 0;
                        })
                    onToggled: {
                        var a = page.selected.slice();
                        (group.modelData.topics || []).forEach(function (t) {
                                var i = a.indexOf(t.name);
                                if (checked && i < 0)
                                    a.push(t.name);
                                if (!checked && i >= 0)
                                    a.splice(i, 1);
                            });
                        page.selected = a;
                    }
                }
                Repeater {
                    model: group.modelData.topics || []
                    delegate: Ui.CheckBox {
                        required property var modelData
                        Layout.fillWidth: true
                        text: modelData.name + " · " + (modelData.type || "")
                        enabled: !page.locked && !(page.distributed.enabled && modelData.name.indexOf("/egohalo/") === 0)
                        checked: page.selected.indexOf(modelData.name) >= 0
                        onToggled: page.toggle(modelData.name, checked)
                    }
                }
            }
        }
    }
    Connections {
        target: workspace
        function onCompleted(key, success) {
            if (key === "action")
                page.starting = false;
            if (key === "topics" && success)
                page.syncTopics();
        }
    }
}
