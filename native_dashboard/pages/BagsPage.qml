import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    property string deleting: ""
    heading: "Rosbags · 录制库"
    description: "查看录制完整性、评分和回放准备状态。"
    RowLayout {
        Ui.Button {
            objectName: "bags-all"
            text: "全部目录"
            highlighted: !!shell && shell.scope === "all"
            onClicked: {
                shell.scope = "all";
                shell.refresh();
            }
        }
        Ui.Button {
            objectName: "bags-current"
            text: "当前目录"
            highlighted: !!shell && shell.scope === "current"
            onClicked: {
                shell.scope = "current";
                shell.refresh();
            }
        }
        Ui.Button {
            objectName: "bags-refresh"
            text: "刷新"
            enabled: !workspace.busy.bags
            onClicked: shell.refresh()
        }
        BusyIndicator {
            running: !!workspace.busy.bags
            visible: running
            implicitWidth: 30
            implicitHeight: 30
        }
    }
    Label {
        text: "暂无录制"
        visible: ((workspace.data.bags || {}).bags || []).length === 0 && !workspace.busy.bags
    }
    Repeater {
        model: (workspace.data.bags || {}).bags || []
        delegate: Ui.Panel {
            required property var modelData
            Layout.fillWidth: true
            ColumnLayout {
                anchors.fill: parent
                Label {
                    text: modelData.name
                    font.bold: true
                    font.pixelSize: 18
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }
                Label {
                    text: (modelData.relative_path || modelData.root || "") + " · " + Number(modelData.duration_s || 0).toFixed(1) + " 秒 · " + (modelData.size_label || "--") + " · " + (modelData.message_count || 0) + " 条消息 · " + (modelData.topic_count || 0) + " 个话题"
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }
                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    Ui.Badge {
                        text: modelData.integrity === true ? "完整性通过" : modelData.integrity === false ? "不完整" : "未检查"
                        tone: modelData.integrity === true ? "success" : modelData.integrity === false ? "danger" : "neutral"
                    }
                    Ui.Badge {
                        text: modelData.scored ? "已评分" : "未评分"
                        tone: modelData.scored ? "success" : "neutral"
                    }
                    Ui.Badge {
                        text: "回放 · " + (modelData.review_state || "pending")
                        tone: modelData.review_state === "ready" ? "accent" : "neutral"
                    }
                }
                RowLayout {
                    Ui.Button {
                        text: "空间回放"
                        enabled: !dashboard.state.recording
                        onClicked: shell.openSpatial(String(modelData.id || modelData.name))
                    }
                    Ui.Button {
                        text: "检查完整性"
                        enabled: !workspace.busy.integrity && !dashboard.state.recording
                        onClicked: workspace.request("integrity", "/api/integrity/run", "POST", {
                                "bag_name": String(modelData.id || modelData.name)
                            })
                    }
                    Ui.Button {
                        objectName: "bag-delete"
                        tone: "danger"
                        text: "删除"
                        enabled: !dashboard.state.recording && !workspace.busy.action
                        onClicked: {
                            page.deleting = String(modelData.id || modelData.name);
                            removeDialog.title = "删除 “" + modelData.name + "”？";
                            removeDialog.open();
                        }
                    }
                }
            }
        }
    }
    IntegrityPanel {
        report: workspace.data.integrity || null
        checking: !!workspace.busy.integrity
        error: workspace.errors.integrity || ""
    }
    Ui.Dialog {
        id: removeDialog
        width: 440
        implicitWidth: 440
        modal: true
        anchors.centerIn: Overlay.overlay
        footer: Item {
            implicitHeight: 52
            Row {
                anchors.right: parent.right
                anchors.rightMargin: 12
                spacing: 8
                Ui.Button {
                    objectName: "delete-confirm"
                    tone: "danger"
                    text: "确认删除"
                    onClicked: removeDialog.accept()
                }
                Ui.Button {
                    text: "取消"
                    onClicked: removeDialog.reject()
                }
            }
        }
        Label {
            text: "永久删除该录制目录，无法撤销。"
        }
        onAccepted: shell.mutate("/api/rosbags/" + encodeURIComponent(page.deleting), {}, "DELETE")
    }
}
