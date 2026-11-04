import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    heading: "Tasks"
    description: "Manage tasks and takes."
    property var taskData: workspace.data.tasks || {}
    property var sessionData: workspace.data.sessions || {}
    property var selected: taskSelect.currentIndex >= 0 ? (taskData.tasks || [])[taskSelect.currentIndex] : null
    property bool locked: !!dashboard.state.recording || !!workspace.busy.action
    property string editing: ""
    function edit(task) {
        editing = task ? task.task_id : "";
        taskId.text = editing;
        taskName.text = task ? task.name : "";
        speech.text = task ? task.speech_name || "" : "";
        instruction.text = task ? task.instruction || "" : "";
        profile.text = task ? task.capture_profile || "dual_arm_umi" : "dual_arm_umi";
        editor.open();
    }
    Ui.Section {
        eyebrow: "01  TASK"
        heading: "Task"
        RowLayout {
            Layout.fillWidth: true
            Ui.TaskPicker {
                id: taskSelect
                objectName: "session-task"
                Layout.fillWidth: true
                model: page.taskData.tasks || []
                activeTaskId: page.taskData.active_task_id || ""
                enabled: !page.locked
            }
            Ui.Button {
                objectName: "session-enter"
                text: "Use task"
                enabled: !page.locked && !!page.selected && page.taskData.active_task_id !== page.selected.task_id
                onClicked: shell.mutate("/api/tasks/" + encodeURIComponent(page.selected.task_id) + "/activate")
            }
            Ui.Button {
                objectName: "session-new"
                highlighted: true
                text: "New task"
                enabled: !page.locked
                onClicked: page.edit(null)
            }
            Ui.Button {
                objectName: "session-edit"
                text: "Edit"
                enabled: !page.locked && !!page.selected
                onClicked: page.edit(page.selected)
            }
            Ui.Button {
                objectName: "session-end"
                text: "End task"
                enabled: !page.locked && !!page.taskData.active_task_id
                onClicked: endDialog.open()
            }
            Ui.Button {
                text: "Refresh"
                onClicked: shell.refresh()
            }
        }
    }
    ResultPanel {
        heading: "Instructions"
        value: page.selected ? {
            "instruction": page.selected.instruction,
            "recording_subdirectory": page.selected.recording_subdirectory,
            "capture_profile": page.selected.capture_profile
        } : {}
    }
    GridLayout {
        columns: page.width < 950 ? 2 : 4
        rowSpacing: 14
        columnSpacing: 14
        Layout.fillWidth: true
        Ui.StatCard {
            label: "Recorded"
            value: String((page.sessionData.stats || {}).recorded_takes || 0)
            detail: "Current task"
            Layout.fillWidth: true
        }
        Ui.StatCard {
            label: "Next take"
            value: String((page.sessionData.stats || {}).next_take_id || 1)
            detail: "Auto increment"
            Layout.fillWidth: true
        }
        Ui.StatCard {
            label: "QC passed"
            value: String((page.sessionData.takes || []).filter(function (t) {
                        return t.operator_valid !== false && (t.quick_qc || {}).state === "pass";
                    }).length)
            detail: "Valid takes"
            Layout.fillWidth: true
        }
        Ui.StatCard {
            label: "Review / Rejected"
            value: String((page.sessionData.takes || []).filter(function (t) {
                        return t.operator_valid === false || (t.quick_qc || {}).state === "suspect";
                    }).length)
            detail: "Quality review"
            Layout.fillWidth: true
        }
    }
    Label {
        text: "Task · " + ((page.sessionData.session || {}).task_name || "No active task")
        color: Ui.Theme.secondary
        font.pixelSize: 16
        font.weight: Font.DemiBold
        Layout.topMargin: 4
    }
    Label {
        text: "No takes yet"
        visible: (page.sessionData.takes || []).length === 0
        color: "#776d5a"
    }
    Repeater {
        model: page.sessionData.takes || []
        delegate: Ui.Panel {
            required property var modelData
            Layout.fillWidth: true
            ColumnLayout {
                anchors.fill: parent
                RowLayout {
                    Label {
                        text: "Take " + modelData.take_id + "  ·  " + (modelData.bag_name || "Waiting for recording")
                        Layout.fillWidth: true
                        font.bold: true
                        wrapMode: Text.Wrap
                    }
                    Ui.Badge {
                        text: modelData.operator_valid === false ? "Rejected" : (modelData.quick_qc || {}).state === "pass" ? "Passed" : "Unchecked"
                        tone: modelData.operator_valid === false ? "danger" : (modelData.quick_qc || {}).state === "pass" ? "success" : "neutral"
                    }
                    Ui.Button {
                        text: "Play"
                        enabled: !!modelData.bag_id && !dashboard.state.recording
                        onClicked: shell.openSpatial(modelData.bag_id)
                    }
                }
                Label {
                    text: Ui.Texts.value(modelData.operator_decision || "Pending review") + " · " + Ui.Texts.value(modelData.reject_reason || modelData.state || "") + " · " + (modelData.anomaly_timeline || []).length + " anomalies"
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }
            }
        }
    }
    Ui.Dialog {
        id: endDialog
        objectName: "end-task-dialog"
        width: 440
        implicitWidth: 440
        title: "End the current task?"
        modal: true
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: shell.mutate("/api/tasks/current/end")
        Label {
            text: "Recorded data will be retained."
        }
    }
    Ui.Dialog {
        id: editor
        objectName: "task-editor"
        title: page.editing ? "Edit task" : "New task"
        modal: true
        width: 480
        anchors.centerIn: Overlay.overlay
        ColumnLayout {
            anchors.fill: parent
            Label {
                text: "Task ID"
            }
            Ui.TextField {
                id: taskId
                objectName: "task-id"
                Layout.fillWidth: true
                enabled: !page.editing
                placeholderText: "e.g. pick_and_place"
            }
            Label {
                text: "Task name"
            }
            Ui.TextField {
                id: taskName
                objectName: "task-name"
                Layout.fillWidth: true
            }
            Label {
                text: "Spoken name"
            }
            Ui.TextField {
                id: speech
                Layout.fillWidth: true
            }
            Label {
                text: "Capture profile"
            }
            Ui.TextField {
                id: profile
                Layout.fillWidth: true
            }
            Label {
                text: "Instructions"
            }
            Ui.TextArea {
                id: instruction
                objectName: "task-instruction"
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                implicitHeight: 100
            }
            Label {
                text: workspace.errors.action || ""
                visible: text.length > 0
                color: "#c2453d"
                Layout.fillWidth: true
                wrapMode: Text.Wrap
            }
            RowLayout {
                Ui.Button {
                    objectName: "task-save"
                    highlighted: true
                    text: "Save"
                    enabled: taskId.text.trim().length > 0 && taskName.text.trim().length > 0 && instruction.text.trim().length > 0 && !workspace.busy.action
                    onClicked: shell.mutate(page.editing ? "/api/tasks/" + encodeURIComponent(page.editing) : "/api/tasks", {
                            "id": taskId.text.trim(),
                            "name": taskName.text.trim(),
                            "speech_name": speech.text.trim(),
                            "capture_profile": profile.text.trim(),
                            "instruction": instruction.text.trim()
                        }, page.editing ? "PUT" : "POST")
                }
                Ui.Button {
                    text: "Cancel"
                    onClicked: editor.close()
                }
            }
        }
    }
    Connections {
        target: workspace
        function onCompleted(key, success) {
            if (key === "action" && success && editor.opened) {
                taskSelect.selectedTaskId = taskId.text.trim();
                editor.close();
            }
        }
    }
}
