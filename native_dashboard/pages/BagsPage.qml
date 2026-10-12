import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
PageFrame {
    id: page; property var shell; property string deleting: ""
    heading: "Rosbags · 录制库"
    description: "查看录制完整性、评分和回放准备状态。"
    RowLayout {
        Button { text: "全部目录"; highlighted: !!shell && shell.scope==="all"; onClicked: { shell.scope="all";shell.refresh() } }
        Button { text: "当前目录"; highlighted: !!shell && shell.scope==="current"; onClicked: { shell.scope="current";shell.refresh() } }
        Button { objectName: "bags-refresh"; text: "刷新"; enabled: !workspace.busy.bags; onClicked: shell.refresh() }
        BusyIndicator { running: !!workspace.busy.bags; visible: running; implicitWidth: 30; implicitHeight: 30 }
    }
    Label { text: "暂无录制"; visible: ((workspace.data.bags || {}).bags || []).length===0 && !workspace.busy.bags }
    Repeater {
        model: (workspace.data.bags || {}).bags || []
        delegate: Frame {
            required property var modelData; Layout.fillWidth: true
            ColumnLayout {
                anchors.fill: parent
                Label { text: modelData.name; font.bold: true; font.pixelSize: 18; Layout.fillWidth: true; wrapMode: Text.Wrap }
                Label { text: (modelData.relative_path || modelData.root || "")+" · "+Number(modelData.duration_s || 0).toFixed(1)+" 秒 · "+(modelData.size_label || "--")+" · "+(modelData.message_count || 0)+" 条消息 · "+(modelData.topic_count || 0)+" 个话题"; Layout.fillWidth: true; wrapMode: Text.Wrap }
                Label { text: (modelData.integrity===true ? "完整" : modelData.integrity===false ? "不完整" : "未检查")+" · "+(modelData.scored ? "已评分" : "未评分")+" · 回放 "+(modelData.review_state || "pending")+" "+(modelData.review_quality || ""); color: modelData.integrity===false ? "#c2453d" : "#776d5a" }
                RowLayout {
                    Button { text: "空间回放"; enabled: !dashboard.state.recording; onClicked: shell.openSpatial(String(modelData.id || modelData.name)) }
                    Button { text: "检查完整性"; enabled: !workspace.busy.integrity && !dashboard.state.recording; onClicked: workspace.request("integrity","/api/integrity/run","POST",{bag_name:String(modelData.id || modelData.name)}) }
                    Button { objectName: "bag-delete"; text: "删除"; enabled: !dashboard.state.recording && !workspace.busy.action; onClicked: { page.deleting=String(modelData.id || modelData.name); removeDialog.title="删除 “"+modelData.name+"”？";removeDialog.open() } }
                }
            }
        }
    }
    ResultPanel { heading: "完整性报告"; value: workspace.data.integrity || {}; visible: !!workspace.data.integrity }
    Dialog {
        id: removeDialog; width: 440; implicitWidth: 440; modal: true; anchors.centerIn: Overlay.overlay
        footer: Item {
            implicitHeight: 52
            Row {
                anchors.right: parent.right; anchors.rightMargin: 12; spacing: 8
                Button { objectName: "delete-confirm"; text: "确认删除"; onClicked: removeDialog.accept() }
                Button { text: "取消"; onClicked: removeDialog.reject() }
            }
        }
        Label { text: "永久删除该录制目录，无法撤销。" }
        onAccepted: shell.mutate("/api/rosbags/"+encodeURIComponent(page.deleting),{},"DELETE")
    }
}
