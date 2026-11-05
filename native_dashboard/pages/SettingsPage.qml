import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    property var settings: workspace.data.settings || {}
    property bool restarting: false
    property int restartTicks: 0
    heading: "Settings"
    description: "Configure masks and services."
    Ui.Button {
        text: "Refresh"
        onClicked: shell.refresh()
    }
    GridLayout {
        columns: page.width < 1050 ? 1 : 2
        Layout.fillWidth: true
        rowSpacing: 20
        columnSpacing: 20
        Ui.Section {
            eyebrow: "LOCALIZATION"
            heading: "Wrist image mask"
            description: "Bottom mask ratio: 0 to less than 1."
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            RowLayout {
                Layout.fillWidth: true
                Ui.TextField {
                    id: mask
                    objectName: "mask-ratio"
                    Layout.fillWidth: true
                    text: String(page.settings.insight3_gripper_mask_height_ratio === undefined ? "" : page.settings.insight3_gripper_mask_height_ratio)
                    validator: DoubleValidator {
                        bottom: 0
                        top: .9999
                        decimals: 4
                        notation: DoubleValidator.StandardNotation
                    }
                }
                Ui.Button {
                    objectName: "mask-save"
                    highlighted: true
                    text: "Save"
                    enabled: mask.acceptableInput && !workspace.busy.action && !dashboard.state.recording
                    onClicked: shell.mutate("/api/settings/insight3-gripper-mask", {
                            "value": Number(mask.text)
                        })
                }
            }
        }
    }
    Ui.Section {
        eyebrow: "Maintenance"
        heading: "Maintenance"
        description: "Preview reconnects after restart."
        Label {
            text: page.restarting ? "Restarting backend…" : page.restartTicks >= 30 ? "Backend unavailable after 30 s." : ""
            color: Ui.Theme.red
            visible: text.length > 0
        }
        Ui.Button {
            objectName: "settings-restart"
            tone: "danger"
            text: "Restart backend"
            enabled: !dashboard.state.recording && !page.restarting && !workspace.busy.action
            onClicked: restartDialog.open()
        }
    }
    Ui.Dialog {
        id: restartDialog
        objectName: "restart-dialog"
        width: 440
        implicitWidth: 440
        title: "Restart the backend?"
        modal: true
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.Ok | Dialog.Cancel
        Label {
            text: "Preview will disconnect briefly."
        }
        onAccepted: {
            page.restarting = true;
            page.restartTicks = 0;
            shell.mutate("/api/settings/restart-backend");
        }
    }
    Timer {
        interval: 1000
        repeat: true
        running: page.restarting
        onTriggered: {
            page.restartTicks++;
            if (page.restartTicks >= 30)
                page.restarting = false;
            workspace.request("health", "/healthz");
        }
    }
    Connections {
        target: workspace
        function onCompleted(key, success) {
            if (key === "health" && success && page.restarting) {
                page.restarting = false;
                shell.refresh();
            }
        }
    }
}
