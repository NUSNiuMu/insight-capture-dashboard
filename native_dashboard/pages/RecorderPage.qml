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
    heading: "Recorder"
    description: distributed.enabled ? "Ego records to SD; Jetson records the wrists. Merge later in Recordings." : "Choose topics and storage, then record."
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
        eyebrow: "01  CAPTURE"
        heading: "Capture"
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
                text: "Use task"
                highlighted: !page.taskActive
                enabled: !page.locked && captureTask.count > 0 && !dashboard.playback
                onClicked: shell.mutate("/api/tasks/" + encodeURIComponent(captureTask.currentValue) + "/activate")
            }
        }
        Label {
            text: page.taskActive ? "Task: " + (((page.status.task_status || {}).task || {}).name || "") : "Select a task first."
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
                placeholderText: "Recording name (optional)"
                enabled: !page.locked
            }
            Ui.Button {
                objectName: "recording-start"
                text: page.starting ? "Starting…" : !page.taskActive ? "Select a task" : "Record"
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
                text: "Stop"
                enabled: !!page.status.recording && !workspace.busy.action && !dashboard.state.captureAction
                onClicked: shell.mutate("/api/recording/stop")
            }
            Ui.Button {
                text: "Refresh"
                onClicked: shell.refresh()
            }
        }
    }
    Ui.Section {
        visible: !!page.distributed.enabled
        heading: "Devices and clocks"
        Label {
            text: "Status: " + ({idle: "Idle", starting: "Starting devices", recording: "Recording", stop_pending: "Waiting for save", saved: "Saved on both devices", copying: "Retrieving Ego data", merging: "Merging by capture time", done: "Merged", error: "Needs review"}[page.distributed.phase] || page.distributed.phase || "Idle")
            Layout.fillWidth: true
            wrapMode: Text.Wrap
        }
        Label {
            text: "Session: " + (page.distributed.session_id || "Assigned on start")
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.muted
        }
        Label {
            text: "Ego SD: " + (page.distributed.ego_path || "/userdata/sdcard/capture") + (page.distributed.remote_saved ? " · Saved" : "")
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.muted
        }
        Label {
            text: "Jetson wrist data: " + (page.distributed.jetson_path || "Assigned on start") + (page.distributed.local_saved ? " · Saved" : "")
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.muted
        }
        Repeater {
            model: Object.keys((page.distributed.clock || {}).devices || {})
            delegate: Label {
                required property string modelData
                property var clock: ((page.distributed.clock || {}).devices || {})[modelData] || {}
                text: modelData + " · " + (clock.offset_ms !== undefined ? "Clock offset " + Number(clock.offset_ms).toFixed(2) + " ms · Bound " + Number(clock.offset_bound_ms).toFixed(2) + " ms" : Ui.Texts.value(clock.error) || "Not measured")
                color: clock.ok ? Ui.Theme.secondary : Ui.Theme.red
                Layout.fillWidth: true
                wrapMode: Text.Wrap
            }
        }
        Label {
            text: "Clock bound ≤ " + ((page.distributed.clock || {}).max_clock_bound_ms || 10) + " ms. Checked throughout recording; NTP does not synchronize exposure."
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: Ui.Theme.muted
        }
        Label {
            text: (page.distributed.clock || {}).checked_at_epoch_s ? "Last checked: " + new Date(page.distributed.clock.checked_at_epoch_s * 1000).toLocaleTimeString() : "Clocks are checked before recording."
            color: Ui.Theme.muted
            Layout.fillWidth: true
        }
        Label {
            text: page.distributed.error || (page.distributed.clock_violations || []).length ? (Ui.Texts.value(page.distributed.error) || "Clock issue · Review timing report") : (page.distributed.quality_issues || []).length ? "Video gaps · Review quality report" : ""
            visible: text.length > 0
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: Ui.Theme.red
        }
    }
    Ui.Section {
        heading: "Recording status"
        RowLayout {
            Layout.fillWidth: true
            Ui.Badge {
                text: page.status.recording ? "Recording" : page.finalizing ? "Saving" : page.status.merge_state === "error" ? "Save failed" : "Idle"
                tone: page.status.recording ? "success" : page.status.merge_state === "error" ? "danger" : page.finalizing ? "accent" : "neutral"
            }
            Label {
                text: page.status.recording ? (page.distributed.enabled ? "Recording to Ego SD and Jetson" : "Recording selected topics") : page.finalizing ? "Saving or merging…" : page.status.merge_state === "saved" ? "Saved. Ready for the next take. Merge in Recordings." : page.status.merge_state === "done" ? "Recording saved" + ((page.status.merge_timings || {}).total_sec !== undefined ? " · Elapsed " + Number(page.status.merge_timings.total_sec).toFixed(1) + " s" : "") : "Ready to record"
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
            text: Ui.Texts.value(page.status.merge_error)
            visible: text.length > 0
            textFormat: Text.PlainText
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
            color: Ui.Theme.red
        }
        Label {
            property var taskStatus: page.status.task_status || {}
            property var stats: taskStatus.stats || {}
            text: taskStatus.active ? ((taskStatus.task || {}).name || "Current task") + " · Recorded " + (stats.recorded_takes || 0) + " takes · Next " + (stats.next_take_id || 1) : "No active task"
            Layout.fillWidth: true
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: Ui.Theme.muted
            font.pixelSize: 13
        }
    }
    Ui.Section {
        eyebrow: "02  STORAGE"
        heading: "Storage"
        Layout.fillWidth: true
        ColumnLayout {
            Layout.fillWidth: true
            Label {
                text: "Recording folder"
                font.bold: true
            }
            Label {
                text: (page.status.storage || {}).active_path || "Loading…"
                Layout.fillWidth: true
                wrapMode: Text.WrapAnywhere
                color: Ui.Theme.ink
                font.pixelSize: 14
            }
            Ui.Badge {
                property var space: page.status.disk_space || {}
                text: space.free_bytes !== undefined ? "Free " + (Number(space.free_bytes) / (1024 * 1024 * 1024)).toFixed(1) + " GB · " + (Number(space.free_ratio) * 100).toFixed(0) + "%" : "Free space unknown"
                tone: space.free_ratio < 0.1 ? "danger" : space.free_ratio < 0.3 ? "accent" : "neutral"
            }
            Label {
                text: "Folder: " + (page.directories.path || "Loading…")
                Layout.fillWidth: true
                wrapMode: Text.WrapAnywhere
                color: Ui.Theme.muted
            }
            RowLayout {
                Ui.Button {
                    text: "Up"
                    enabled: !!page.directories.parent && !page.locked
                    onClicked: page.browse(page.directories.parent)
                }
                Ui.Button {
                    objectName: "storage-select"
                    text: "Use folder"
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
            text: "Topics · Selected " + page.selected.length + " / " + page.known.length
            font.bold: true
        }
        Ui.Button {
            text: "Select all"
            enabled: !page.locked
            onClicked: page.selected = page.known.slice()
        }
        Ui.Button {
            text: "Clear"
            enabled: !page.locked
            onClicked: page.selected = []
        }
        Ui.Button {
            text: "Defaults"
            enabled: !page.locked
            onClicked: page.selected = (page.catalog.default_selected_topics || []).slice()
        }
    }
    Repeater {
        model: (page.catalog.cameras || []).concat((page.catalog.other || []).length ? [{
                    "label": "Other",
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
                    text: (group.modelData.label || group.modelData.namespace) + (page.distributed.enabled && group.modelData.namespace === "egohalo" ? " (SD recording)" : "")
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
