import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

ColumnLayout {
    id: panel
    property var shell
    property var status: workspace.data.savedRecordings || ({})
    property var phaseNames: ({saved: "Saved · Not merged", copying: "Retrieving", merging: "Merging", done: "Merged", error: "Merge failed · Originals retained"})
    visible: !!status.enabled
    Label {
        text: "Saved recordings"
        font.bold: true
        font.pixelSize: 18
    }
    Label {
        text: "Keep recording after saving. Retrieve and merge here later."
        Layout.fillWidth: true
        wrapMode: Text.Wrap
    }
    Label {
        text: workspace.errors.savedRecordings || ""
        visible: text.length > 0
        color: Ui.Theme.red
    }
    Repeater {
        model: panel.status.recordings || []
        delegate: Ui.Panel {
            required property var modelData
            Layout.fillWidth: true
            ColumnLayout {
                anchors.fill: parent
                Label {
                    text: modelData.task_name + " · Take " + modelData.take_id + " · " + (panel.phaseNames[modelData.phase] || modelData.phase)
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }
                Label {
                    text: Ui.Texts.value(modelData.error) || ""
                    visible: text.length > 0
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    color: Ui.Theme.red
                }
                Ui.Button {
                    objectName: "merge-" + modelData.session_id
                    text: "Retrieve and merge"
                    visible: modelData.phase === "saved" || modelData.phase === "error"
                    enabled: !dashboard.state.recording && !panel.status.busy && !workspace.busy.action
                    onClicked: panel.shell.mutate("/api/postprocessing/merge", {session_id: modelData.session_id})
                }
            }
        }
    }
}
