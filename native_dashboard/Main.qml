import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick3D
import QtQuick3D.AssetUtils
import Insight.Native 1.0
import "pages"
import "ui" as Ui

ApplicationWindow {
    id: root
    width: 1600; height: 1000; visible: true
    visibility: startFullScreen ? Window.FullScreen : Window.Windowed
    title: "Insight Capture"
    color: Ui.Theme.paper
    property string currentPage: "spatial"
    property real pageSwitchStartedAt: 0
    property real pageFirstFrameAt: 0
    property bool lastRecording: false
    property bool taskActive: !!(dashboard.state.task || {}).active
    property string captureAction: dashboard.state.captureAction || ""
    property bool captureLocked: !!dashboard.state.recording || !!dashboard.state.recordingBusy || captureAction.length > 0
    property string lastTaskState: ""
    onCurrentPageChanged: {
        pageSwitchStartedAt=Date.now();pageFirstFrameAt=0
        dashboard.setSpatialActive(currentPage==="spatial")
    }
    Connections {
        target: dashboard
        function onStateChanged() {
            var recording=!!dashboard.state.recording
            var task=JSON.stringify(dashboard.state.task || {})
            if(recording!==root.lastRecording)workspace.invalidate()
            else if(task!==root.lastTaskState){workspace.invalidate("tasks");workspace.invalidate("sessions")}
            root.lastRecording=recording;root.lastTaskState=task
        }
    }
    property bool spatialActive: currentPage === "spatial"
    property var frameSizes: ({})
    property int previewFps: dashboard.targetFps
    property real sceneFps: view.renderStats.fps
    property real cameraChromeHeight: 32 + cameraTitleMetrics.height + cameraStatusMetrics.height
    FontMetrics { id: cameraTitleMetrics; font: root.font }
    FontMetrics { id: cameraStatusMetrics; font.family: root.font.family; font.pixelSize: 11 }
    // Match the web ArcRotateCamera after reflecting its Z axis.
    property real yaw: 90 - 1.2 * 180 / Math.PI
    property real pitch: 1.1 * 180 / Math.PI - 90
    property real distance: 580
    property real playTime: 0
    property bool playPaused: false
    property string confirmAction: ""
    property string confirmValue: ""
    font.family: Ui.Theme.bodyFont
    font.pixelSize: 14
    palette.button: "#fffaf0"
    palette.base: "#fffaf0"
    palette.highlight: "#b87813"
    palette.buttonText: "#494438"
    palette.text: "#25251f"
    function confirm(action, value, description) {
        confirmAction = action; confirmValue = value; confirmDialog.title = description; confirmDialog.open()
    }
    function spatialDiagnostics() { var result=[]; for(var i=0;i<poseRepeater.count;++i)result.push(poseRepeater.objectAt(i).diagnostics()); return result }
    function controlPositions() {
        var result={page:root.currentPage,visibleTexts:root.visibleTexts(root.contentItem)}; var items={scene:scenePanel,cameraDock:cameraDock,record:recordButton,activate:activateButton,load:loadButton,live:liveButton,pause:pauseButton,check:checkButton,timeline:timeline}
        for(var key in items) { var item=items[key];var p=item.mapToItem(null,item.width/2,item.height/2);result[key]={x:p.x,y:p.y,width:item.width,height:item.height,enabled:item.enabled} }
        var nav = root.findChildrenForDiagnostics(sidebar)
        for(var j=0;j<nav.length;++j){var button=nav[j];var pos=button.mapToItem(null,button.width/2,button.height/2);result[button.objectName]={x:pos.x,y:pos.y,width:button.width,height:button.height,enabled:button.enabled}}
        var controls=root.findChildrenForDiagnostics(pageLoader).concat(root.findChildrenForDiagnostics(Overlay.overlay))
        for(var k=0;k<controls.length;++k){var c=controls[k];var cp=c.mapToItem(null,c.width/2,c.height/2);result[c.objectName]={x:cp.x,y:cp.y,width:c.width,height:c.height,enabled:c.enabled,visible:c.visible,text:c.text===undefined ? "" : c.text,checked:c.checked===undefined ? false : c.checked}}
        return result
    }
    function visibleTexts(item) {
        if (!item || !item.visible || item.opacity === 0) return []
        var texts = typeof item.text === "string" && item.text.trim() ? [item.text] : []
        for (var i=0;i<item.children.length;++i) texts=texts.concat(visibleTexts(item.children[i]))
        return texts
    }
    function findChildrenForDiagnostics(item) {
        var result=[]
        if(item.objectName)result.push(item)
        for(var i=0;i<item.children.length;++i)result=result.concat(findChildrenForDiagnostics(item.children[i]))
        return result
    }
    function navigationDiagnostics() { return {created:pageLoader.cacheStats,last:pageLoader.lastSwitch} }
    function videoDiagnostics() { var result=[]; allVideos(function(v){var d=v.diagnostics();d.viewportWidth=v.width;d.viewportHeight=v.height;result.push(d)}); return result }
    function allVideos(fn) {
        for (var i=0; i<cameraRepeater.count; ++i) cameraRepeater.itemAt(i).eachVideo(fn)
    }
    function cameraGroups() {
        var groups=[], positions={}
        for (var i=0; i<dashboard.cameras.length; ++i) {
            var camera=dashboard.cameras[i]
            var key=dashboard.playback ? camera.name : (camera.device || camera.name)
            if (positions[key] === undefined) {
                positions[key]=groups.length
                groups.push({name:key, row:camera.row, column:camera.column, streams:[]})
            }
            groups[positions[key]].streams.push(camera)
        }
        for (var j=0; j<groups.length; ++j) {
            var group=groups[j]
            group.columns=Math.min(2, group.streams.length)
            if (group.columns>1) { group.row=0; group.column=1 }
            group.rowSpan=groups.length===3 && group.column>0 ? 2 : 1
        }
        return groups
    }
    function cameraGroupWidth(group, height) {
        var rows=cameraGroups().reduce(function(count, item) { return Math.max(count, item.row+item.rowSpan) }, 1)
        var groupHeight=(height-(rows-1)*cameraDock.rowSpacing)/rows*group.rowSpan+(group.rowSpan-1)*cameraDock.rowSpacing
        var streamRows=Math.ceil(group.streams.length/group.columns)
        var imageHeight=Math.max(1, (groupHeight-(streamRows-1)*cameraDock.rowSpacing)/streamRows-cameraChromeHeight)
        var widths=[]
        for (var i=0; i<group.columns; ++i) widths.push(0)
        for (var j=0; j<group.streams.length; ++j) {
            var stream=group.streams[j]
            var size=root.frameSizes[stream.name] || stream
            widths[j%group.columns]=Math.max(widths[j%group.columns], imageHeight*(size.width || 1)/(size.height || 1))
        }
        return widths.reduce(function(sum, width) { return sum+width }, 0)+(group.columns-1)*cameraDock.columnSpacing
    }
    function cameraDockWidth(height) {
        var groups=cameraGroups(), widths=[]
        for (var i=0; i<groups.length; ++i) {
            var group=groups[i]
            var width=(cameraGroupWidth(group, height)-(group.columns-1)*cameraDock.columnSpacing)/group.columns
            for (var j=0; j<group.columns; ++j) widths[group.column+j]=Math.max(widths[group.column+j] || 0, width)
        }
        var natural=widths.reduce(function(sum, width) { return sum+width }, 0)+Math.max(0,widths.length-1)*cameraDock.columnSpacing
        return Math.min(natural, (spatialWorkspace.width-spatialWorkspace.columnSpacing)*2/3)
    }
    Shortcut { sequence: "Escape"; onActivated: root.visibility = Window.Windowed }
    Shortcut { sequence: "F11"; onActivated: root.visibility = root.visibility === Window.FullScreen ? Window.Windowed : Window.FullScreen }
    Ui.Dialog {
        id: confirmDialog; objectName: "confirm-dialog"; anchors.centerIn: parent; modal: true; width: 400
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: dashboard.command(root.confirmAction, root.confirmValue)
        Label { text: "Apply to the current device."; padding: 16 }
    }
    RowLayout {
        anchors.fill: parent; spacing: 0
        Sidebar {
            id: sidebar; Layout.preferredWidth: compact ? 64 : 232; Layout.fillHeight: true
            compact: root.width < 1200; currentPage: root.currentPage
            onSelected: function(page){root.currentPage=page}
        }
        ColumnLayout {
        Layout.fillWidth: true; Layout.fillHeight: true; Layout.margins: 14; spacing: 12
        Rectangle {
            Layout.fillWidth: true; Layout.preferredHeight: 56; color: "transparent"
            RowLayout {
                anchors.fill: parent; anchors.margins: 15; spacing: 18
                Label { text: "WORKSPACE /"; font.family: Ui.Theme.monoFont; font.pixelSize: 11; font.letterSpacing: 1; color: Ui.Theme.faint }
                Label { text: root.spatialActive ? "Capture" : (pageLoader.titles[root.currentPage] || ""); font.pixelSize: 14; font.weight: Font.DemiBold; color: Ui.Theme.ink }
                Rectangle { width: 9; height: 9; radius: 5; color: dashboard.connected ? "#70ac82" : "#c27a6a" }
                Label { text: dashboard.connected ? "Connected" : "Disconnected"; font.pixelSize: 12; color: Ui.Theme.muted }
                Item { Layout.fillWidth: true }
                Ui.Badge { text: dashboard.playback ? "Playback" : "Live"; tone: "accent"; visible: root.spatialActive }
                Ui.Button { objectName: "copy-diagnostics"; text: "Copy diagnostics"; onClicked: dashboard.copyDiagnostics(root.videoDiagnostics()) }
                Ui.ComboBox { visible: root.spatialActive; model: [25,30]; currentIndex: dashboard.targetFps===30?1:0; onActivated: root.previewFps=Number(currentText); implicitWidth: 85 }
                Label { visible: root.spatialActive; font.family: Ui.Theme.monoFont; font.pixelSize: 11; text: "3D " + root.sceneFps.toFixed(1) + " FPS"; color: "#776d5a" }
                Ui.Button { text: "Fullscreen"; onClicked: root.visibility = root.visibility === Window.FullScreen ? Window.Windowed : Window.FullScreen }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 18; Layout.minimumHeight: 18; Layout.maximumHeight: 18
            BusyIndicator { running: Object.keys(workspace.busy).some(function(k){return workspace.busy[k]}); visible: running; implicitWidth: 18; implicitHeight: 18 }
            Label { text: Object.keys(workspace.errors).map(function(k){return Ui.Texts.value(workspace.errors[k])}).join(" · ") || Ui.Texts.value(dashboard.state.captureError) || (!root.taskActive && dashboard.connected && !dashboard.playback ? "Select a task to record." : ""); textFormat: Text.PlainText; color: "#c2453d"; elide: Text.ElideRight; Layout.fillWidth: true; ToolTip.visible: errorMouse.containsMouse; ToolTip.text: text; MouseArea { id: errorMouse; anchors.fill: parent; hoverEnabled: true } }
        }
        WorkspacePages {
            id: pageLoader; Layout.fillWidth: true; Layout.fillHeight: true
            visible: !root.spatialActive; page: root.spatialActive ? "" : root.currentPage
            onOpenSpatial: function(bag){root.currentPage="spatial";root.playPaused=false;dashboard.command("playback",bag)}
        }
        GridLayout {
            visible: root.spatialActive
            id: spatialWorkspace
            Layout.fillWidth: true; Layout.fillHeight: true
            columns: 2
            rowSpacing: 12; columnSpacing: 12
            GridLayout {
                id: cameraDock
                Layout.row: 0; Layout.column: 0
                Layout.preferredWidth: root.cameraDockWidth(spatialWorkspace.height)
                Layout.minimumWidth: 0
                Layout.preferredHeight: -1
                Layout.fillWidth: false; Layout.fillHeight: true
                columns: root.cameraGroups().reduce(function(columns, group) { return Math.max(columns, group.column + group.columns) }, 1)
                rowSpacing: 10; columnSpacing: 12
                Repeater {
                    id: cameraRepeater; model: root.cameraGroups()
                    delegate: GridLayout {
                        id: cameraGroup; required property var modelData
                        columns: modelData.columns
                        rowSpacing: cameraDock.rowSpacing; columnSpacing: cameraDock.columnSpacing
                        Layout.row: modelData.row
                        Layout.column: modelData.column
                        Layout.columnSpan: modelData.columns
                        Layout.rowSpan: modelData.rowSpan
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0
                        Layout.preferredWidth: root.cameraGroupWidth(modelData, cameraDock.height)
                        Layout.preferredHeight: Layout.rowSpan
                        function eachVideo(fn) { for(var j=0;j<streamRepeater.count;++j)fn(streamRepeater.itemAt(j).player) }
                        Repeater {
                            id: streamRepeater; model: cameraGroup.modelData.streams
                            delegate: Rectangle {
                                id: cameraCard; required property var modelData
                                property alias player: video
                                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0; Layout.preferredWidth: 1; Layout.preferredHeight: 1; color: Ui.Theme.surface; radius: 12; border.color: Ui.Theme.line
                                Label { id: cameraTitle; anchors.top: parent.top; anchors.left: parent.left; anchors.margins: 10; text: Ui.Texts.value(cameraCard.modelData.label); width: parent.width-92; elide: Text.ElideRight; font.bold: true; color: "#514b3e" }
                                Label {
                                    id: cameraStatus
                                    property var localization: dashboard.localization[cameraCard.modelData.device || cameraCard.modelData.name] || {}
                                    anchors.top: cameraTitle.bottom; anchors.left: parent.left; anchors.margins: 10
                                    text: dashboard.playback ? "Playback" : "Image: " + (dashboard.cameraAvailability[cameraCard.modelData.name] === true ? "Receiving" : "No input") + " · " + (localization.label || "Waiting for pose")
                                    elide: Text.ElideRight; width: parent.width-20; font.pixelSize: 11
                                    color: dashboard.playback ? "#51778e" : (localization.color || "#84775f")
                                    ToolTip.visible: statusHover.hovered
                                    ToolTip.text: localization.detail || text
                                    HoverHandler { id: statusHover }
                                }
                                Label { anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 10; text: video.renderedFps.toFixed(1) + " FPS"; color: "#a27723"; font.bold: true }
                                Rectangle {
                                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: cameraStatus.bottom; anchors.bottom: parent.bottom
                                    anchors.topMargin: 6; anchors.bottomMargin: 6; color: "#17130c"; radius: 8
                                    Label { anchors.centerIn: parent; width: parent.width-20; text: video.status; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter; color: "#a39885"; font.pixelSize: 12; visible: !dashboard.playback && video.renderedFps<=0 }
                                }
                                NativeVideo {
                                    id: video; active: root.spatialActive; visible: root.spatialActive && !confirmDialog.visible && !task.popup.visible && !bag.popup.visible; anchors.left: parent.left; anchors.right: parent.right; anchors.top: cameraStatus.bottom; anchors.bottom: parent.bottom; anchors.topMargin: 6; anchors.bottomMargin: 6
                                    sourceSize: Qt.size(cameraCard.modelData.width || 0, cameraCard.modelData.height || 0)
                                    sourceOnline: dashboard.playback || dashboard.cameraAvailability[cameraCard.modelData.name] !== false
                                    cameraName: cameraCard.modelData.name; endpoint: cameraCard.modelData.endpoint; nativeEndpoint: cameraCard.modelData.nativeEndpoint || ""; media: cameraCard.modelData.media; targetFps: root.previewFps
                                    onStatsChanged: { if (dashboard.playback) { root.playTime = position; dashboard.setPlaybackTime(position) } }
                                    onFrameSizeChanged: {
                                        var sizes=Object.assign({}, root.frameSizes)
                                        sizes[cameraName]={width:frameSize.width, height:frameSize.height}
                                        root.frameSizes=sizes
                                    }
                                }
                            }
                        }
                    }
                }
                Label { visible: cameraRepeater.count===0; text: "Waiting for cameras…"; color: "#776d5a" }
            }
            Rectangle {
                id: scenePanel
                Layout.preferredWidth: 1
                Layout.row: 0; Layout.column: 1
                Layout.preferredHeight: -1
                Layout.minimumWidth: 0; Layout.fillHeight: true; Layout.fillWidth: true; color: Ui.Theme.surface; radius: 12; border.color: Ui.Theme.line
                ColumnLayout {
                    anchors.fill: parent; spacing: 0
                    Flow {
                        Layout.fillWidth: true; Layout.margins: 12; spacing: 8
                        Label { height: 42; verticalAlignment: Text.AlignVCenter; text: "3D view"; font.bold: true; color: "#514b3e" }
                        Label { height: 42; verticalAlignment: Text.AlignVCenter; text: "Map points: " + (dashboard.state.mapPoints || 0); color: "#84775f" }
                        Item { Layout.fillWidth: true }
                        Ui.CheckBox { text: "Keep trails"; checked: dashboard.keepTrail; onToggled: dashboard.keepTrail = checked }
                        Ui.Button { text: "Clear trails"; onClicked: dashboard.command("clear") }
                        Ui.Button { text: "Reset view"; onClicked: { root.yaw=90-1.2*180/Math.PI;root.pitch=1.1*180/Math.PI-90;root.distance=580;orbit.position=Qt.vector3d(0,90,0) } }
                    }
                    Item {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        View3D {
                            id: view; anchors.fill: parent
                            environment: SceneEnvironment { clearColor: Ui.Theme.paper; backgroundMode: SceneEnvironment.Color; antialiasingMode: SceneEnvironment.NoAA }
                            Node {
                                id: orbit; position: Qt.vector3d(0,90,0); eulerRotation: Qt.vector3d(root.pitch,root.yaw,0)
                                PerspectiveCamera { id: camera; z: root.distance; fieldOfView: 0.8 * 180 / Math.PI; clipNear: 1; clipFar: 8000 }
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
                                    function diagnostics() { return {name:modelData.name, coordinateFrame:modelData.coordinateFrame, status:avatar.status, error:avatar.errorString, boundsMin:[avatar.bounds.minimum.x,avatar.bounds.minimum.y,avatar.bounds.minimum.z], boundsMax:[avatar.bounds.maximum.x,avatar.bounds.maximum.y,avatar.bounds.maximum.z], position:[modelData.displayPosition.x,modelData.displayPosition.y,modelData.displayPosition.z], sourcePosition:[modelData.position.x,modelData.position.y,modelData.position.z], scale:modelData.modelScale, rotation:[modelData.displayRotation.x,modelData.displayRotation.y,modelData.displayRotation.z,modelData.displayRotation.scalar], sourceRotation:[modelData.rotation.x,modelData.rotation.y,modelData.rotation.z,modelData.rotation.scalar], modelRotation:[modelData.modelRotation.x,modelData.modelRotation.y,modelData.modelRotation.z,modelData.modelRotation.scalar]} }
                                    Model {
                                        geometry: poseRoot.modelData.trail
                                        visible: poseRoot.modelData.trailEnabled && poseRoot.modelData.pointCount > 1
                                        materials: CustomMaterial {
                                            shadingMode: CustomMaterial.Unshaded
                                            cullMode: Material.NoCulling
                                            vertexShader: "qrc:/trail.vert"
                                            fragmentShader: "qrc:/trail.frag"
                                            // Unshaded output uses display RGB, without QColor's linear conversion.
                                            property vector4d trailColor: poseRoot.modelData.coordinateFrame === "local_vio" ? Qt.vector4d(.67,.45,.1,1) : Qt.vector4d(poseRoot.modelData.color.r,
                                                poseRoot.modelData.color.g, poseRoot.modelData.color.b, 1)
                                            property real lineWidth: poseRoot.modelData.role === "head" ? 6 : 5
                                            property vector2d viewportSize: Qt.vector2d(view.width, view.height)
                                        }
                                    }
                                    Node {
                                        visible: poseRoot.modelData.visible || (dashboard.playback && poseRoot.modelData.pointCount > 0); position: poseRoot.modelData.displayPosition; rotation: poseRoot.modelData.displayRotation
                                        Node {
                                            position: poseRoot.modelData.modelOffset; rotation: poseRoot.modelData.modelRotation
                                            scale: Qt.vector3d(poseRoot.modelData.modelScale,poseRoot.modelData.modelScale,poseRoot.modelData.modelScale)
                                            RuntimeLoader {
                                                id: avatar; source: poseRoot.modelData.modelSource
                                                position: Qt.vector3d(-(bounds.minimum.x+bounds.maximum.x)/2, -(bounds.minimum.y+bounds.maximum.y)/2, -(bounds.minimum.z+bounds.maximum.z)/2)
                                            }
                                        }
                                        Model { source: "#Sphere"; visible: avatar.status !== RuntimeLoader.Success; scale: Qt.vector3d(.07,.07,.07); materials: DefaultMaterial { diffuseColor: poseRoot.modelData.color } }
                                    }
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
                        Label { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.margins: 12; text: "Drag to orbit · Right-drag to pan · Scroll to zoom"; font.pixelSize: 11; color: "#9c8f75" }
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.margins: 10
                        Repeater {
                            model: dashboard.poses
                            delegate: Ui.CheckBox { required property var modelData; text: Ui.Texts.value(modelData.role) + " · " + (modelData.coordinateFrame === "local_vio" ? "Local VIO" : modelData.pointCount + " points") + (dashboard.playback && !modelData.visible && modelData.pointCount > 0 ? " · Pose gap" : ""); checked: modelData.trailEnabled; onToggled: modelData.setTrail(checked) }
                        }
                        Item { Layout.fillWidth: true }
                    }
                    Label {
                        visible: !dashboard.playback && dashboard.poses.some(function(p) { return p.coordinateFrame === "local_vio" })
                        Layout.fillWidth: true; Layout.margins: 10; wrapMode: Text.Wrap
                        text: "Local VIO · Unaligned: device positions are not in the same frame."
                        font.pixelSize: 11; color: "#aa741b"
                    }
                }
            }
        }
        Rectangle {
            visible: root.spatialActive
            Layout.fillWidth: true; Layout.preferredHeight: controls.implicitHeight+22; color: Ui.Theme.surface; radius: 12; border.color: Ui.Theme.line
            ColumnLayout {
                id: controls; anchors.fill: parent; anchors.margins: 11; spacing: 8
                RowLayout {
                    Label { text: "Task"; color: "#776d5a" }
                    Ui.TaskPicker { id: task; Layout.preferredWidth: 190; model: dashboard.tasks; activeTaskId: ((dashboard.state.task || {}).task || {}).task_id || ""; enabled: !root.captureLocked }
                    Ui.Button { id: activateButton; text: "Use task"; highlighted: !root.taskActive; enabled: task.count>0&&!dashboard.state.recording&&!dashboard.playback&&!dashboard.state.recordingBusy&&!root.captureAction; onClicked: dashboard.command("task",task.currentValue) }
                    Ui.Button { text: "End task"; enabled: !root.captureLocked && root.taskActive; onClicked: dashboard.command("endTask") }
                    Rectangle { width: 1; height: 25; color: "#ddd1b9" }
                    Ui.Button { id: recordButton; tone: dashboard.state.recording ? "danger" : "primary"; text: root.captureAction === "starting" ? "Starting…" : root.captureAction === "stopping" ? "Stopping…" : dashboard.state.recording ? "Stop" : dashboard.state.recordingBusy ? "Saving…" : !root.taskActive ? "Select a task" : "Record"; highlighted: true; enabled: (dashboard.state.recording||dashboard.connected&&root.taskActive)&&!dashboard.playback&&!dashboard.state.recordingBusy&&!root.captureAction; onClicked: dashboard.command(dashboard.state.recording?"stop":"record") }
                    Ui.Button { text: "Reject last take"; tone: "danger"; enabled: !dashboard.state.recording; onClicked: root.confirm("reject","operator_rejected","Reject the last take?") }
                    Item { Layout.fillWidth: true }
                    Ui.Button { id: checkButton; text: "Check"; onClicked: dashboard.command("check") }
                    Ui.Button { text: "New map"; enabled: !dashboard.state.recording&&!dashboard.playback; onClicked: root.confirm("reset","","Reset the current map?") }
                }
                RowLayout {
                    Label { text: "Playback"; color: "#776d5a" }
                    Ui.ComboBox { id: bag; Layout.fillWidth: true; model: dashboard.bags; textRole: "name"; valueRole: "selector"; enabled: !dashboard.state.recording }
                    Ui.Button { text: "Refresh"; onClicked: dashboard.command("refreshBags") }
                    Ui.Button { id: loadButton; text: "Load"; enabled: bag.count>0&&!dashboard.state.recording&&!dashboard.playback; onClicked: dashboard.command("playback",bag.currentValue) }
                    Ui.Button { id: pauseButton; text: root.playPaused ? "Resume" : "Pause"; enabled: dashboard.playback; onClicked: { root.playPaused=!root.playPaused;root.allVideos(function(v){v.paused=root.playPaused}) } }
                    Ui.Slider { id: timeline; Layout.preferredWidth: 190; from: 0; to: Math.max(1,dashboard.duration); value: root.playTime; enabled: dashboard.playback; onMoved: root.allVideos(function(v){v.seek(timeline.value)}) }
                    Label { text: dashboard.playback ? root.playTime.toFixed(1)+" / "+dashboard.duration.toFixed(1)+" s" : Ui.Texts.value(dashboard.state.playbackState || "idle"); color: "#84775f" }
                    Ui.Button { id: liveButton; text: "Live"; enabled: dashboard.playback || ["preparing", "playing"].indexOf(dashboard.state.playbackState) >= 0; onClicked: { root.playPaused=false;dashboard.command("live") } }
                }
                Label { text: root.captureAction === "starting" ? "Checking devices and clocks…" : root.captureAction === "stopping" ? "Stopping recording…" : dashboard.state.recording ? "● Recording  " + ((dashboard.state.distributed || {}).enabled ? "Ego SD + Jetson · " : "") + (dashboard.state.output || "") : dashboard.state.recordingBusy ? "Saving…" : Ui.Texts.value(dashboard.message); Layout.fillWidth: true; elide: Text.ElideRight; color: dashboard.state.recording ? "#bf604d" : "#84775f" }
            }
        }
    }
    }
}
