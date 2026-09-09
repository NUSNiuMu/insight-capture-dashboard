import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick3D
import QtQuick3D.AssetUtils
import Insight.Native 1.0

ApplicationWindow {
    id: root
    width: 1600; height: 1000; visible: true
    visibility: startFullScreen ? Window.FullScreen : Window.Windowed
    title: "Insight · 原生采集台"
    color: "#eee7d5"
    property int previewFps: dashboard.targetFps
    property real sceneFps: view.renderStats.fps
    property real yaw: -25
    property real pitch: -22
    property real distance: 300
    property real playTime: 0
    property bool playPaused: false
    property string confirmAction: ""
    property string confirmValue: ""
    font.family: "Noto Sans CJK SC"
    function confirm(action, value, description) {
        confirmAction = action; confirmValue = value; confirmDialog.title = description; confirmDialog.open()
    }
    function spatialDiagnostics() { var result=[]; for(var i=0;i<poseRepeater.count;++i)result.push(poseRepeater.objectAt(i).diagnostics()); return result }
    function controlPositions() {
        var result={}; var items={record:recordButton,activate:activateButton,load:loadButton,live:liveButton,pause:pauseButton,check:checkButton,timeline:timeline}
        for(var key in items) { var item=items[key];var p=item.mapToItem(null,item.width/2,item.height/2);result[key]={x:p.x,y:p.y,enabled:item.enabled} }
        return result
    }
    function videoDiagnostics() { var result=[]; allVideos(function(v){result.push(v.diagnostics())}); return result }
    function allVideos(fn) {
        for (var i=0; i<cameraRepeater.count; ++i) fn(cameraRepeater.itemAt(i).player)
    }
    Shortcut { sequence: "Escape"; onActivated: root.visibility = Window.Windowed }
    Shortcut { sequence: "F11"; onActivated: root.visibility = root.visibility === Window.FullScreen ? Window.Windowed : Window.FullScreen }
    Dialog {
        id: confirmDialog; anchors.centerIn: parent; modal: true; width: 400
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: dashboard.command(root.confirmAction, root.confirmValue)
        Label { text: "操作将应用于当前设备。"; padding: 16 }
    }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 14; spacing: 12
        Rectangle {
            Layout.fillWidth: true; height: 72; color: "#fffaf0"; radius: 10
            RowLayout {
                anchors.fill: parent; anchors.margins: 15; spacing: 18
                Label { text: "INSIGHT"; font.pixelSize: 24; font.bold: true; color: "#b87813" }
                Label { text: "原生采集台"; font.pixelSize: 21; color: "#494438" }
                Rectangle { width: 9; height: 9; radius: 5; color: dashboard.connected ? "#70ac82" : "#c27a6a" }
                Label { text: dashboard.connected ? "姿态在线" : "姿态未连接"; color: "#776d5a" }
                Item { Layout.fillWidth: true }
                Label { text: dashboard.playback ? "回放" : "实时"; font.bold: true; color: "#9a711e" }
                ComboBox { model: [25,30]; currentIndex: dashboard.targetFps===30?1:0; onActivated: root.previewFps=Number(currentText); implicitWidth: 75 }
                Label { text: "3D " + root.sceneFps.toFixed(1) + " FPS"; color: "#776d5a" }
                Button { text: "全屏"; onClicked: root.visibility = root.visibility === Window.FullScreen ? Window.Windowed : Window.FullScreen }
            }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12
            ColumnLayout {
                Layout.preferredWidth: dashboard.playback ? root.width * 0.38 : Math.max(280, root.width * 0.20)
                Layout.maximumWidth: Layout.preferredWidth; Layout.minimumWidth: 280
                Layout.fillHeight: true; spacing: 10
                Repeater {
                    id: cameraRepeater; model: dashboard.cameras
                    delegate: Rectangle {
                        id: cameraCard; required property var modelData
                        property alias player: video
                        Layout.fillWidth: true; Layout.fillHeight: true; color: "#fffaf0"; radius: 9
                        Label { id: cameraTitle; anchors.top: parent.top; anchors.left: parent.left; anchors.margins: 10; text: cameraCard.modelData.label; font.bold: true; color: "#514b3e" }
                        Label { id: cameraStatus; anchors.top: cameraTitle.bottom; anchors.left: parent.left; anchors.margins: 10; text: video.status; elide: Text.ElideRight; width: parent.width-90; font.pixelSize: 11; color: "#84775f" }
                        Label { anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 10; text: "输出 " + video.renderedFps.toFixed(1) + " fps"; color: "#a27723"; font.bold: true }
                        NativeVideo {
                            id: video; visible: !confirmDialog.visible && !task.popup.visible && !bag.popup.visible; anchors.left: parent.left; anchors.right: parent.right; anchors.top: cameraStatus.bottom; anchors.bottom: parent.bottom; anchors.margins: 6
                            cameraName: cameraCard.modelData.name; endpoint: cameraCard.modelData.endpoint; media: cameraCard.modelData.media; targetFps: root.previewFps
                            onStatsChanged: { if (dashboard.playback) { root.playTime = position; dashboard.setPlaybackTime(position) } }
                        }
                    }
                }
                Label { visible: cameraRepeater.count===0; text: "等待相机列表…"; color: "#776d5a" }
            }
            Rectangle {
                Layout.minimumWidth: 500; Layout.fillHeight: true; Layout.fillWidth: true; color: "#f6efdf"; radius: 10
                ColumnLayout {
                    anchors.fill: parent; spacing: 0
                    RowLayout {
                        Layout.fillWidth: true; Layout.margins: 12
                        Label { text: "空间视图"; font.bold: true; color: "#514b3e" }
                        Label { text: (dashboard.state.mapPoints || 0) + " 个地图点"; color: "#84775f" }
                        Item { Layout.fillWidth: true }
                        CheckBox { text: "保留轨迹"; checked: dashboard.keepTrail; onToggled: dashboard.keepTrail = checked }
                        Button { text: "清轨迹"; onClicked: dashboard.command("clear") }
                        Button { text: "复位视角"; onClicked: { root.yaw=-25;root.pitch=-22;root.distance=300;orbit.position=Qt.vector3d(0,60,0) } }
                    }
                    Item {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        View3D {
                            id: view; anchors.fill: parent
                            environment: SceneEnvironment { clearColor: "#eee7d5"; backgroundMode: SceneEnvironment.Color; antialiasingMode: SceneEnvironment.NoAA }
                            Node {
                                id: orbit; position: Qt.vector3d(0,60,0); eulerRotation: Qt.vector3d(root.pitch,root.yaw,0)
                                PerspectiveCamera { id: camera; z: root.distance; clipNear: 1; clipFar: 8000 }
                            }
                            camera: camera
                            DirectionalLight { eulerRotation.x: -45; eulerRotation.y: -25; brightness: 1.2; ambientColor: "#8d8d8d" }
                            Repeater3D {
                                model: 21
                                delegate: Node {
                                    required property int index
                                    Model { source: "#Cube"; position: Qt.vector3d((index-10)*40,-1,0); scale: Qt.vector3d(0.006,0.003,8); materials: DefaultMaterial { diffuseColor: "#c4b89b"; lighting: DefaultMaterial.NoLighting } }
                                    Model { source: "#Cube"; position: Qt.vector3d(0,-1,(index-10)*40); scale: Qt.vector3d(8,0.003,0.006); materials: DefaultMaterial { diffuseColor: "#c4b89b"; lighting: DefaultMaterial.NoLighting } }
                                }
                            }
                            Model { source: "#Cube"; x: 50; scale: Qt.vector3d(1,0.014,0.014); materials: DefaultMaterial { diffuseColor: "#cc7666"; lighting: DefaultMaterial.NoLighting } }
                            Model { source: "#Cube"; y: 50; scale: Qt.vector3d(0.014,1,0.014); materials: DefaultMaterial { diffuseColor: "#79ac7b"; lighting: DefaultMaterial.NoLighting } }
                            Model { source: "#Cube"; z: -50; scale: Qt.vector3d(0.014,0.014,1); materials: DefaultMaterial { diffuseColor: "#75a6c1"; lighting: DefaultMaterial.NoLighting } }
                            Repeater3D {
                                id: poseRepeater; model: dashboard.poses
                                delegate: Node {
                                    id: poseRoot; required property var modelData
                                    function diagnostics() { return {name:modelData.name, status:avatar.status, error:avatar.errorString, boundsMin:[avatar.bounds.minimum.x,avatar.bounds.minimum.y,avatar.bounds.minimum.z], boundsMax:[avatar.bounds.maximum.x,avatar.bounds.maximum.y,avatar.bounds.maximum.z], position:[modelData.position.x,modelData.position.y,modelData.position.z], scale:modelData.modelScale} }
                                    Model { geometry: poseRoot.modelData.trail; visible: poseRoot.modelData.trailEnabled && poseRoot.modelData.pointCount > 1; materials: DefaultMaterial { diffuseColor: poseRoot.modelData.color; lighting: DefaultMaterial.NoLighting } }
                                    Node {
                                        visible: poseRoot.modelData.visible; position: poseRoot.modelData.position; rotation: poseRoot.modelData.rotation
                                        Node {
                                            position: poseRoot.modelData.modelOffset; rotation: poseRoot.modelData.modelRotation
                                            scale: Qt.vector3d(poseRoot.modelData.modelScale,poseRoot.modelData.modelScale,poseRoot.modelData.modelScale)
                                            RuntimeLoader {
                                                id: avatar; source: poseRoot.modelData.modelSource
                                                position: Qt.vector3d(-(bounds.minimum.x+bounds.maximum.x)/2, -(bounds.minimum.y+bounds.maximum.y)/2, -(bounds.minimum.z+bounds.maximum.z)/2)
                                                onStatusChanged: { if (status===RuntimeLoader.Success) { dashboard.updateGripper(avatar,poseRoot.modelData.opening) } }
                                            }
                                        }
                                        Model { source: "#Sphere"; visible: avatar.status !== RuntimeLoader.Success; scale: Qt.vector3d(.07,.07,.07); materials: DefaultMaterial { diffuseColor: poseRoot.modelData.color } }
                                    }
                                    Connections { target: poseRoot.modelData; function onChanged() { if(poseRoot.modelData.role!=="head" && avatar.status===RuntimeLoader.Success)dashboard.updateGripper(avatar,poseRoot.modelData.opening) } }
                                }
                            }
                        }
                        MouseArea {
                            anchors.fill: parent; acceptedButtons: Qt.LeftButton | Qt.RightButton
                            property real lastX; property real lastY
                            onPressed: function(mouse) { lastX=mouse.x; lastY=mouse.y }
                            onPositionChanged: function(mouse) { if(!pressed)return;var dx=mouse.x-lastX,dy=mouse.y-lastY;if(pressedButtons&Qt.RightButton)orbit.position=Qt.vector3d(orbit.x-dx*.5,orbit.y+dy*.5,orbit.z);else {root.yaw-=dx*.3;root.pitch=Math.max(-89,Math.min(89,root.pitch-dy*.3))}lastX=mouse.x;lastY=mouse.y }
                            onWheel: function(wheel) { root.distance=Math.max(120,Math.min(2400,root.distance*Math.pow(.9,wheel.angleDelta.y/120))) }
                        }
                        Label { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.margins: 12; text: "左键旋转 · 右键平移 · 滚轮缩放"; font.pixelSize: 11; color: "#9c8f75" }
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.margins: 10
                        Repeater {
                            model: dashboard.poses
                            delegate: CheckBox { required property var modelData; text: modelData.role + " · " + modelData.pointCount + " 点"; checked: modelData.trailEnabled; onToggled: modelData.setTrail(checked) }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }
        }
        Rectangle {
            Layout.fillWidth: true; Layout.preferredHeight: controls.implicitHeight+22; color: "#fffaf0"; radius: 10
            ColumnLayout {
                id: controls; anchors.fill: parent; anchors.margins: 11; spacing: 8
                RowLayout {
                    Label { text: "采集任务"; color: "#776d5a" }
                    ComboBox { id: task; Layout.preferredWidth: 190; model: dashboard.tasks; textRole: "name"; valueRole: "task_id"; enabled: !dashboard.state.recording }
                    Button { id: activateButton; text: "进入任务"; enabled: task.count>0&&!dashboard.state.recording&&!dashboard.playback; onClicked: dashboard.command("task",task.currentValue) }
                    Button { text: "结束任务"; enabled: !dashboard.state.recording; onClicked: dashboard.command("endTask") }
                    Rectangle { width: 1; height: 25; color: "#ddd1b9" }
                    Button { id: recordButton; text: dashboard.state.recording ? "停止录制" : "开始录制"; highlighted: true; enabled: (dashboard.state.recording||dashboard.connected)&&!dashboard.playback; onClicked: dashboard.command(dashboard.state.recording?"stop":"record") }
                    Button { text: "作废最近一条"; enabled: !dashboard.state.recording; onClicked: root.confirm("reject","operator_rejected","确认作废最近一条记录？") }
                    Item { Layout.fillWidth: true }
                    Button { id: checkButton; text: "采集检查"; onClicked: dashboard.command("check") }
                    Button { text: "新建地图"; enabled: !dashboard.state.recording&&!dashboard.playback; onClicked: root.confirm("reset","","确认重置当前地图？") }
                }
                RowLayout {
                    Label { text: "录制回放"; color: "#776d5a" }
                    ComboBox { id: bag; Layout.fillWidth: true; model: dashboard.bags; textRole: "name"; valueRole: "selector"; enabled: !dashboard.state.recording }
                    Button { text: "刷新"; onClicked: dashboard.command("refreshBags") }
                    Button { id: loadButton; text: "载入"; enabled: bag.count>0&&!dashboard.state.recording&&!dashboard.playback; onClicked: dashboard.command("playback",bag.currentValue) }
                    Button { id: pauseButton; text: root.playPaused ? "继续" : "暂停"; enabled: dashboard.playback; onClicked: { root.playPaused=!root.playPaused;root.allVideos(function(v){v.paused=root.playPaused}) } }
                    Slider { id: timeline; Layout.preferredWidth: 190; from: 0; to: Math.max(1,dashboard.duration); value: root.playTime; enabled: dashboard.playback; onMoved: root.allVideos(function(v){v.seek(timeline.value)}) }
                    Label { text: dashboard.playback ? root.playTime.toFixed(1)+" / "+dashboard.duration.toFixed(1)+" s" : (dashboard.state.playbackState || "idle"); color: "#84775f" }
                    Button { id: liveButton; text: "返回实时"; enabled: dashboard.playback; onClicked: { root.playPaused=false;dashboard.command("live") } }
                }
                Label { text: dashboard.state.recording ? "● 录制中  " + (dashboard.state.output || "") : dashboard.message; Layout.fillWidth: true; elide: Text.ElideRight; color: dashboard.state.recording ? "#bf604d" : "#84775f" }
            }
        }
    }
}
