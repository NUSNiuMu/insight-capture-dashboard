import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

ColumnLayout {
    id: panel
    property var shell
    property var status: workspace.data.savedRecordings || ({})
    property var phaseNames: ({saved: "已保存 · 待合包", copying: "回传中", merging: "合包中", done: "已合包", error: "合包失败 · 原始数据保留"})
    visible: !!status.enabled
    Label {
        text: "两端原始录制 · 手动回传合包"
        font.bold: true
        font.pixelSize: 18
    }
    Label {
        text: "EgoHalo 和 Jetson 各自保存后即可继续采集；结束采集后在这里合包。"
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
                    text: modelData.task_name + " · 第 " + modelData.take_id + " 条 · " + (panel.phaseNames[modelData.phase] || modelData.phase)
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }
                Label {
                    text: modelData.error || ""
                    visible: text.length > 0
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    color: Ui.Theme.red
                }
                Ui.Button {
                    objectName: "merge-" + modelData.session_id
                    text: "回传并合包"
                    visible: modelData.phase === "saved" || modelData.phase === "error"
                    enabled: !dashboard.state.recording && !panel.status.busy && !workspace.busy.action
                    onClicked: panel.shell.mutate("/api/postprocessing/merge", {session_id: modelData.session_id})
                }
            }
        }
    }
}
