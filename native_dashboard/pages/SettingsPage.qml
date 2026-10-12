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
    heading: "Settings · 设置"
    description: "管理相机功能、语音提示与采集工作台。"
    Ui.Button {
        text: "刷新设置"
        onClicked: shell.refresh()
    }
    GridLayout {
        columns: page.width < 1000 ? 1 : 2
        Layout.fillWidth: true
        rowSpacing: 20
        columnSpacing: 20
        Repeater {
            model: page.settings.poses || []
            delegate: Ui.Section {
                id: camera
                required property var modelData
                Layout.fillWidth: true
                eyebrow: "CAMERA FEATURES"
                heading: camera.modelData.label || camera.modelData.name
                Ui.Badge {
                    text: ({
                            "head": "头部相机",
                            "left_hand": "左手相机",
                            "right_hand": "右手相机"
                        })[camera.modelData.role] || camera.modelData.role
                    tone: "accent"
                }
                Ui.CheckBox {
                    objectName: "gripper-toggle"
                    text: "夹爪追踪"
                    visible: !!camera.modelData.gripper_tracking_available
                    checked: !!camera.modelData.gripper_tracking_enabled
                    enabled: !workspace.busy.action && !dashboard.state.recording
                    onToggled: shell.mutate("/api/settings/gripper-tracking", {
                            "name": camera.modelData.name,
                            "enabled": checked
                        })
                }
                Ui.CheckBox {
                    objectName: "overlay-toggle"
                    text: "手部叠加"
                    visible: !!camera.modelData.hand_overlay_available
                    checked: !!camera.modelData.hand_overlay_enabled
                    enabled: !workspace.busy.action && !dashboard.state.recording
                    onToggled: shell.mutate("/api/settings/hand-overlay", {
                            "name": camera.modelData.name,
                            "enabled": checked
                        })
                }
                Label {
                    text: "该相机暂无可切换的功能"
                    visible: !camera.modelData.gripper_tracking_available && !camera.modelData.hand_overlay_available
                    color: Ui.Theme.muted
                    font.pixelSize: 13
                }
            }
        }
    }
    GridLayout {
        columns: page.width < 1050 ? 1 : 2
        Layout.fillWidth: true
        rowSpacing: 20
        columnSpacing: 20
        Ui.Section {
            eyebrow: "MASK CONFIGURATION"
            heading: "夹爪遮罩"
            description: "设置 Insight3 遮罩覆盖比例，范围为 0 至 1（不含 1）。"
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            RowLayout {
                Layout.fillWidth: true
                Ui.TextField {
                    id: mask
                    objectName: "mask-ratio"
                    Layout.fillWidth: true
                    text: String(page.settings.insight3_gripper_mask_height_ratio === undefined ? .2 : page.settings.insight3_gripper_mask_height_ratio)
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
                    text: "保存"
                    enabled: mask.acceptableInput && !workspace.busy.action && !dashboard.state.recording
                    onClicked: shell.mutate("/api/settings/insight3-gripper-mask", {
                            "value": Number(mask.text)
                        })
                }
            }
        }
        Ui.Section {
            eyebrow: "VOICE FEEDBACK"
            heading: "语音提示"
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            Label {
                text: page.audio.available ? ((page.audio.backend || "") + " · " + (page.audio.playback_label || "")) : page.audio.error || "语音服务不可用"
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
                    text: "试听"
                    enabled: !!page.audio.available && !workspace.busy.voice
                    onClicked: workspace.request("voice", "/api/settings/voice-sample", "POST")
                }
            }
        }
    }
    Ui.Section {
        eyebrow: "MAINTENANCE"
        heading: "系统维护"
        description: "重启会暂时断开相机预览，完成后自动检查连接。"
        Label {
            text: page.restarting ? "等待后端重启…" : page.restartTicks >= 30 ? "30 秒内未恢复，请检查后端。" : ""
            color: Ui.Theme.red
            visible: text.length > 0
        }
        Ui.Button {
            objectName: "settings-restart"
            tone: "danger"
            text: "重启后端"
            enabled: !dashboard.state.recording && !page.restarting && !workspace.busy.action
            onClicked: restartDialog.open()
        }
    }
    Ui.Dialog {
        id: restartDialog
        width: 440
        implicitWidth: 440
        title: "确认重启后端？"
        modal: true
        anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.Ok | Dialog.Cancel
        Label {
            text: "相机预览会暂时断开。"
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
