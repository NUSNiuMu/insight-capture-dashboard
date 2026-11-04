import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    property var status: workspace.data.optimization || {}
    property var trajectory: workspace.data.trajectories || {}
    property bool running: status.state === "running" || !!workspace.busy.action
    property string loadedRun: ""
    heading: "Optimize"
    description: "Compare VIO with reconstructed trajectories."
    function load(name) {
        if (!name)
            return;
        loadedRun = name;
        workspace.request("trajectories", "/api/optimization/trajectories?run_name=" + encodeURIComponent(name));
    }
    Ui.Section {
        eyebrow: "01  INPUT"
        heading: "Reconstruction"
        GridLayout {
            columns: 2
            Layout.fillWidth: true
            enabled: !page.running
            Label {
                text: "Recording"
            }
            BagPicker {
                id: bag
                Layout.fillWidth: true
            }
            Label {
                text: "Camera"
            }
            Ui.ComboBox {
                id: camera
                model: ((workspace.data.cameras || {}).cameras || []).map(function(camera) {
                    return {name: camera.name, label: Ui.Texts.value(camera.label)};
                })
                textRole: "label"
                valueRole: "name"
                Layout.fillWidth: true
            }
            Label {
                text: "Stream"
            }
            Ui.ComboBox {
                id: stream
                model: [{label: "Color (compressed)", value: "color_compressed"},
                    {label: "Color (raw)", value: "color"},
                    {label: "Left IR", value: "infra1"},
                    {label: "Right IR", value: "infra2"}]
                textRole: "label"
                valueRole: "value"
                Layout.fillWidth: true
            }
            Label {
                text: "Run name"
            }
            Ui.TextField {
                id: runName
                Layout.fillWidth: true
                placeholderText: "Automatic"
            }
        }
        RowLayout {
            Ui.Button {
                objectName: "optimization-start"
                text: "Run"
                highlighted: true
                enabled: !!bag.selector && camera.count > 0 && !page.running && !dashboard.state.recording
                onClicked: shell.mutate("/api/optimization/start", {
                        "bag_name": bag.selector,
                        "camera_name": camera.currentValue,
                        "stream_type": stream.currentValue,
                        "run_name": runName.text.trim() || bag.selector
                    })
            }
            Ui.Button {
                objectName: "optimization-stop"
                tone: "danger"
                text: "Stop"
                enabled: page.status.state === "running" && !workspace.busy.action
                onClicked: shell.mutate("/api/optimization/stop")
            }
            Ui.Button {
                text: "View log"
                enabled: !!(page.status.result || {}).colmap_log
                onClicked: workspace.request("optLog", page.status.result.colmap_log, "GET", {}, true)
            }
            Ui.Button {
                text: "Refresh"
                onClicked: shell.refresh()
            }
        }
    }
    Ui.ProgressBar {
        Layout.fillWidth: true
        value: {
            if (page.status.state === "done")
                return 1;
            var ranges = [[0, 0], [.01, .07], [.07, .15], [.15, .90], [.90, .98]];
            var r = ranges[page.status.step || 0] || [0, 1];
            return r[0] + (r[1] - r[0]) * (page.status.step === 3 ? Number(page.status.sub_progress || 0) : .5);
        }
    }
    ResultPanel {
        value: page.status
    }
    Ui.Panel {
        Layout.fillWidth: true
        visible: (page.status.log_tail || []).length > 0
        Label {
            text: (page.status.log_tail || []).join("\n")
            font.family: Ui.Theme.monoFont
            font.pixelSize: 12
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            Layout.fillWidth: true
            color: "#776d5a"
        }
    }
    Label {
        text: workspace.data.optLog || ""
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        Layout.fillWidth: true
        visible: text.length > 0
    }
    Ui.Section {
        eyebrow: "03  COMPARISON"
        heading: "Trajectories"
        RowLayout {
            Layout.fillWidth: true
            Ui.ComboBox {
                id: saved
                Layout.fillWidth: true
                model: (workspace.data.runs || {}).runs || []
                textRole: "run_name"
            }
            Ui.Button {
                objectName: "optimization-load"
                text: "Load"
                enabled: saved.count > 0
                onClicked: page.load(saved.currentText)
            }
        }
        Label {
            text: "VIO (red) · Reconstruction (green) · " + page.loadedRun
        }
        NativePlot {
            Layout.fillWidth: true
            lines: [{
                    "points": (page.trajectory.vio || []).map(function (p) {
                            return [p[0], p[2], p[1]];
                        }),
                    "color": "#c2453d"
                }, {
                    "points": (page.trajectory.colmap || []).map(function (p) {
                            return [p[0], p[2], p[1]];
                        }),
                    "color": "#1f8a55"
                }]
        }
    }
    Connections {
        target: workspace
        function onCompleted(key, success) {
            if (key === "optimization" && success && page.status.state === "done" && page.status.run_name && page.status.run_name !== page.loadedRun) {
                page.load(page.status.run_name);
                workspace.request("runs", "/api/optimization/runs");
            }
        }
    }
}
