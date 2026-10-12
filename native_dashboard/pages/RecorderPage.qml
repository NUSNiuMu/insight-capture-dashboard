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
    property bool finalizing: status.merge_state === "merging" || status.merge_state === "finalizing"
    property bool locked: !!status.recording || finalizing || !!workspace.busy.action
    heading: "Recorder · 录制"
    description: "选择 ROS 话题和存储目录，查看录制与保存状态。完整性检查请前往评分或录制库。"
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
            Ui.TextField {
                id: name
                objectName: "recording-name"
                Layout.fillWidth: true
                placeholderText: "可选录制名称"
                enabled: !page.locked
            }
            Ui.Button {
                objectName: "recording-start"
                text: "开始录制"
                highlighted: true
                enabled: !page.locked && page.selected.length > 0 && !dashboard.playback
                onClicked: {
                    var body = {
                        "topics": page.selected
                    };
                    if (name.text.trim())
                        body.bag_name = name.text.trim();
                    shell.mutate("/api/recording/start", body);
                }
            }
            Ui.Button {
                objectName: "recording-stop"
                tone: "danger"
                text: "停止录制"
                enabled: !!page.status.recording && !workspace.busy.action
                onClicked: shell.mutate("/api/recording/stop")
            }
            Ui.Button {
                text: "刷新"
                onClicked: shell.refresh()
            }
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
                text: page.status.recording ? "正在采集所选话题" : page.finalizing ? "正在合并录制，请等待保存完成…" : page.status.merge_state === "done" ? "录制已保存" + ((page.status.merge_timings || {}).total_sec !== undefined ? " · 耗时 " + Number(page.status.merge_timings.total_sec).toFixed(1) + " 秒" : "") : "准备开始录制"
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
                    text: group.modelData.label || group.modelData.namespace
                    enabled: !page.locked
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
                        enabled: !page.locked
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
            if (key === "topics" && success)
                page.syncTopics();
        }
    }
}
