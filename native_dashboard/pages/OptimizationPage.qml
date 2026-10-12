import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
PageFrame {
    id: page; property var shell
    property var status: workspace.data.optimization || {}
    property var trajectory: workspace.data.trajectories || {}
    property bool running: status.state==="running" || !!workspace.busy.action
    property string loadedRun: ""
    heading: "Optimization · 优化"
    description: "图像重建、Sim3 对齐，比较 VIO 与 COLMAP 轨迹。"
    function load(name) { if(!name)return;loadedRun=name;workspace.request("trajectories","/api/optimization/trajectories?run_name="+encodeURIComponent(name)) }
    GridLayout {
        columns: 2; Layout.fillWidth: true; enabled: !page.running
        Label { text: "录制" }
        BagPicker { id: bag; Layout.fillWidth: true }
        Label { text: "相机" }
        ComboBox { id: camera; model: (workspace.data.cameras || {}).cameras || []; textRole: "label"; valueRole: "name"; Layout.fillWidth: true }
        Label { text: "图像流" }
        ComboBox { id: stream; model: ["color_compressed","color","infra1","infra2"]; Layout.fillWidth: true }
        Label { text: "运行名称" }
        TextField { id: runName; Layout.fillWidth: true; placeholderText: "自动生成" }
    }
    RowLayout {
        Button { objectName: "optimization-start"; text: "运行优化"; highlighted: true; enabled: !!bag.selector && camera.count>0 && !page.running && !dashboard.state.recording; onClicked: shell.mutate("/api/optimization/start",{bag_name:bag.selector,camera_name:camera.currentValue,stream_type:stream.currentText,run_name:runName.text.trim() || bag.selector}) }
        Button { objectName: "optimization-stop"; text: "停止"; enabled: page.status.state==="running" && !workspace.busy.action; onClicked: shell.mutate("/api/optimization/stop") }
        Button { text: "查看重建日志"; enabled: !!(page.status.result || {}).colmap_log; onClicked: workspace.request("optLog",page.status.result.colmap_log,"GET",{},true) }
        Button { text: "刷新"; onClicked: shell.refresh() }
    }
    ProgressBar { Layout.fillWidth: true; value: {if(page.status.state==="done")return 1;var ranges=[[0,0],[.01,.07],[.07,.15],[.15,.90],[.90,.98]];var r=ranges[page.status.step || 0] || [0,1];return r[0]+(r[1]-r[0])*(page.status.step===3 ? Number(page.status.sub_progress || 0) : .5)} }
    ResultPanel { value: page.status }
    Label { text: (page.status.log_tail || []).join("\n"); textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true; color: "#776d5a" }
    Label { text: workspace.data.optLog || ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true; visible: text.length>0 }
    RowLayout {
        Layout.fillWidth: true
        ComboBox { id: saved; Layout.fillWidth: true; model: (workspace.data.runs || {}).runs || []; textRole: "run_name" }
        Button { objectName: "optimization-load"; text: "载入轨迹"; enabled: saved.count>0; onClicked: page.load(saved.currentText) }
    }
    Label { text: "VIO（红色） · COLMAP（绿色） · "+page.loadedRun }
    NativePlot { Layout.fillWidth: true; lines: [{points:(page.trajectory.vio || []).map(function(p){return [p[0],p[2],p[1]]}),color:"#c2453d"},{points:(page.trajectory.colmap || []).map(function(p){return [p[0],p[2],p[1]]}),color:"#1f8a55"}] }
    Connections { target: workspace; function onCompleted(key,success) {if(key==="optimization" && success && page.status.state==="done" && page.status.run_name && page.status.run_name!==page.loadedRun){page.load(page.status.run_name);workspace.request("runs","/api/optimization/runs")}} }
}
