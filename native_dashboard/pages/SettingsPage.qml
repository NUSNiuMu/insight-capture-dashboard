import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
PageFrame {
    id: page; property var shell; property var settings: workspace.data.settings || {}
    property var audio: settings.voice_audio || {}
    property bool restarting: false
    property int restartTicks: 0
    property int pendingVolume: -1
    heading: "Settings · 设置"
    description: "相机功能、语音提示和系统维护。"
    Button { text: "刷新设置"; onClicked: shell.refresh() }
    Repeater {
        model: page.settings.poses || []
        delegate: Frame {
            id: camera; required property var modelData; Layout.fillWidth: true
            ColumnLayout {
                anchors.fill: parent
                Label { text: (camera.modelData.label || camera.modelData.name)+" · "+camera.modelData.role; font.bold: true; font.pixelSize: 18 }
                CheckBox { objectName: "gripper-toggle"; text: "夹爪追踪"; visible: !!camera.modelData.gripper_tracking_available; checked: !!camera.modelData.gripper_tracking_enabled; enabled: !workspace.busy.action && !dashboard.state.recording; onToggled: shell.mutate("/api/settings/gripper-tracking",{name:camera.modelData.name,enabled:checked}) }
                CheckBox { objectName: "overlay-toggle"; text: "手部叠加"; visible: !!camera.modelData.hand_overlay_available; checked: !!camera.modelData.hand_overlay_enabled; enabled: !workspace.busy.action && !dashboard.state.recording; onToggled: shell.mutate("/api/settings/hand-overlay",{name:camera.modelData.name,enabled:checked}) }
            }
        }
    }
    RowLayout {
        Label { text: "Insight3 夹爪遮罩高度比例" }
        TextField { id: mask; objectName: "mask-ratio"; text: String(page.settings.insight3_gripper_mask_height_ratio===undefined ? .2 : page.settings.insight3_gripper_mask_height_ratio); validator: DoubleValidator { bottom: 0; top: .9999; decimals: 4; notation: DoubleValidator.StandardNotation } }
        Button { objectName: "mask-save"; text: "保存"; enabled: mask.acceptableInput && !workspace.busy.action && !dashboard.state.recording; onClicked: shell.mutate("/api/settings/insight3-gripper-mask",{value:Number(mask.text)}) }
    }
    Frame {
        Layout.fillWidth: true
        ColumnLayout {
            anchors.fill: parent
            Label { text: "语音提示音量"; font.bold: true }
            Label { text: page.audio.available ? ((page.audio.backend || "")+" · "+(page.audio.playback_label || "")) : page.audio.error || "语音服务不可用"; Layout.fillWidth: true; wrapMode: Text.Wrap }
            RowLayout {
                Layout.fillWidth: true
                Slider { id: volume; objectName: "voice-volume"; Layout.fillWidth: true; from: 0; to: 100; value: Number(page.audio.volume_percent || 0); enabled: !!page.audio.available; onMoved: {page.pendingVolume=Math.round(value);voiceTimer.restart()} }
                Label { text: Math.round(volume.value)+"%" }
                Button { objectName: "voice-sample"; text: "试听"; enabled: !!page.audio.available && !workspace.busy.voice; onClicked: workspace.request("voice","/api/settings/voice-sample","POST") }
            }
        }
    }
    Label { text: page.restarting ? "等待后端重启…" : page.restartTicks>=30 ? "30 秒内未恢复，请检查后端。" : ""; color: "#c2453d"; visible: text.length>0 }
    Button { objectName: "settings-restart"; text: "重启后端"; enabled: !dashboard.state.recording && !page.restarting && !workspace.busy.action; onClicked: restartDialog.open() }
    Dialog {
        id: restartDialog; width: 440; implicitWidth: 440; title: "确认重启后端？"; modal: true; anchors.centerIn: Overlay.overlay; standardButtons: Dialog.Ok | Dialog.Cancel
        Label { text: "相机预览会暂时断开。" }
        onAccepted: {page.restarting=true;page.restartTicks=0;shell.mutate("/api/settings/restart-backend")}
    }
    Timer { id: voiceTimer; interval: 200; onTriggered: {if(workspace.busy.volume){restart();return}if(page.pendingVolume<0)return;var v=page.pendingVolume;page.pendingVolume=-1;workspace.request("volume","/api/settings/voice-volume","POST",{volume_percent:v})} }
    Timer { interval: 1000; repeat: true; running: page.restarting; onTriggered: {page.restartTicks++;if(page.restartTicks>=30)page.restarting=false;workspace.request("health","/healthz")} }
    Connections {
        target: workspace
        function onCompleted(key,success) {
            if(key==="health" && success && page.restarting){page.restarting=false;shell.refresh()}
            if(key==="volume" && page.pendingVolume>=0)voiceTimer.restart()
            else if((key==="voice" || key==="volume") && success)workspace.request("settings","/api/settings")
        }
    }
}
