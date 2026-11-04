import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    property string deleting: ""
    Connections {
        target: workspace
        function onCompleted(key, success) { if (key === "action") page.deleting = "" }
    }
    heading: "Recordings"
    description: "Review and manage recordings."
    SavedRecordings {
        Layout.fillWidth: true
        shell: page.shell
    }
    RowLayout {
        Ui.Button {
            objectName: "bags-all"
            text: "All folders"
            highlighted: !!shell && shell.scope === "all"
            onClicked: {
                shell.scope = "all";
                shell.refresh();
            }
        }
        Ui.Button {
            objectName: "bags-current"
            text: "Current folder"
            highlighted: !!shell && shell.scope === "current"
            onClicked: {
                shell.scope = "current";
                shell.refresh();
            }
        }
        Ui.Button {
            objectName: "bags-refresh"
            text: "Refresh"
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
        text: "No recordings"
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
                    text: (modelData.relative_path || modelData.root || "") + " · " + Number(modelData.duration_s || 0).toFixed(1) + " s · " + (modelData.size_label || "--") + " · " + (modelData.message_count || 0) + " messages · " + (modelData.topic_count || 0) + " topics"
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }
                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    Ui.Badge {
                        text: modelData.integrity === true ? "Integrity passed" : modelData.integrity === false ? "Incomplete" : "Unchecked"
                        tone: modelData.integrity === true ? "success" : modelData.integrity === false ? "danger" : "neutral"
                    }
                    Ui.Badge {
                        text: modelData.scored ? "Scored" : "Not scored"
                        tone: modelData.scored ? "success" : "neutral"
                    }
                    Ui.Badge {
                        text: "Playback · " + Ui.Texts.value(modelData.review_state || "pending")
                        tone: modelData.review_state === "ready" ? "accent" : "neutral"
                    }
                }
                RowLayout {
                    Ui.Button {
                        text: "Play"
                        enabled: !dashboard.state.recording
                        onClicked: shell.openSpatial(String(modelData.id || modelData.name))
                    }
                    Ui.Button {
                        text: "Check integrity"
                        enabled: !workspace.busy.integrity && !dashboard.state.recording
                        onClicked: workspace.request("integrity", "/api/integrity/run", "POST", {
                                "bag_name": String(modelData.id || modelData.name)
                            })
                    }
                    Ui.Button {
                        objectName: "bag-delete"
                        tone: "danger"
                        text: workspace.busy.action && page.deleting === String(modelData.id || modelData.name) ? "Deleting…" : "Delete"
                        enabled: !dashboard.state.recording && !workspace.busy.action
                        onClicked: {
                            page.deleting = String(modelData.id || modelData.name);
                            removeDialog.title = "Delete “" + modelData.name + "”?";
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
                    text: "Delete"
                    onClicked: removeDialog.accept()
                }
                Ui.Button {
                    text: "Cancel"
                    onClicked: removeDialog.reject()
                }
            }
        }
        Label {
            text: "Permanently delete this recording?"
        }
        onAccepted: shell.mutate("/api/rosbags/" + encodeURIComponent(page.deleting), {}, "DELETE")
        onRejected: page.deleting = ""
    }
}
