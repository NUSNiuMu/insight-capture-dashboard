import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    property var settings: workspace.data.settings || {}
    property var audio: settings.voice_audio || {}
    property bool restarting: false
    property int restartTicks: 0
    property int pendingVolume: -1
    heading: "Settings"
    description: "Configure masks, audio and services."
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
        Ui.Section {
            eyebrow: "Audio prompts"
            heading: "Audio prompts"
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            Label {
                text: page.audio.available ? (Ui.Texts.value(page.audio.backend || "") + " · " + (page.audio.playback_label || "")) : Ui.Texts.value(page.audio.error || "Voice service unavailable")
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: page.audio.available ? Ui.Theme.muted : Ui.Theme.red
                font.pixelSize: 13
            }
            RowLayout {
                Layout.fillWidth: true
                Ui.Slider {
                    id: volume
                    objectName: "voice-volume"
                    Layout.fillWidth: true
                    from: 0
                    to: 100
                    value: Number(page.audio.volume_percent || 0)
                    enabled: !!page.audio.available
                    onMoved: {
                        page.pendingVolume = Math.round(value);
                        voiceTimer.restart();
                    }
                }
                Label {
                    text: Math.round(volume.value) + "%"
                    font.family: Ui.Theme.monoFont
                    color: Ui.Theme.teal
                }
                Ui.Button {
                    objectName: "voice-sample"
                    text: "Test audio"
                    enabled: !!page.audio.available && !workspace.busy.voice
                    onClicked: workspace.request("voice", "/api/settings/voice-sample", "POST")
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
        id: voiceTimer
        interval: 200
        onTriggered: {
            if (workspace.busy.volume) {
                restart();
                return;
            }
            if (page.pendingVolume < 0)
                return;
            var v = page.pendingVolume;
            page.pendingVolume = -1;
            workspace.request("volume", "/api/settings/voice-volume", "POST", {
                    "volume_percent": v
                });
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
            if (key === "volume" && page.pendingVolume >= 0)
                voiceTimer.restart();
            else if ((key === "voice" || key === "volume") && success)
                workspace.request("settings", "/api/settings");
        }
    }
}
