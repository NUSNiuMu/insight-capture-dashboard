import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Insight.Native 1.0
import "HandTracking.js" as Tracking
PageFrame {
    id: page; property var shell
    heading: "Hand pose · 手部姿态"
    description: "WiLoR 提取、保存结果、原生三维骨架浏览。坐标位于相机空间。"
    property var status: workspace.data.handpose || {}
    property var capability: ((workspace.data.capabilities || {}).methods || {}).wilor || {}
    property var frames: []
    property string loadedUrl: ""
    property string previewUrl: ""
    property int index: 0
    property real timeMs: 0
    property real lastTick: 0
    property var referenceBounds: null
    property bool playing: false
    property var edges: [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]]
    function load(url) { if(!url)return;loadedUrl=url;playing=false;workspace.request("handFrames",url) }
    function seek(t) {timeMs=t;var lo=0,hi=frames.length-1;while(lo<hi){var mid=Math.ceil((lo+hi)/2);if(Number(frames[mid].t || 0)<=t)lo=mid;else hi=mid-1}index=lo}
    function lines() {
        var hands=frames.length ? (frames[index].h || []) : []
        return hands.map(function(h){var points=[];for(var i=0;i+2<(h.p || []).length;i+=3)points.push([Number(h.p[i]),-Number(h.p[i+1]),-Number(h.p[i+2])]);return {points:points,edges:page.edges,joints:true,color:h.c==="L" ? "#79adc2" : "#cf7f6f"}})
    }
    RowLayout {
        Layout.fillWidth: true
        BagPicker { id: bag; Layout.fillWidth: true; enabled: page.status.state!=="running" }
        Button { objectName: "handpose-start"; text: "开始提取"; enabled: !!bag.selector && !!page.capability.available && page.status.state!=="running" && !workspace.busy.action && !dashboard.state.recording; onClicked: shell.mutate("/api/handpose/start",{bag_name:bag.selector,method:"wilor"}) }
        Button { objectName: "handpose-stop"; text: "停止"; enabled: page.status.state==="running" && !workspace.busy.action; onClicked: shell.mutate("/api/handpose/stop") }
        Button { text: "刷新"; onClicked: shell.refresh() }
    }
    Label { text: page.capability.available ? "WiLoR 已就绪" : "WiLoR 不可用："+(page.capability.missing || ["等待运行环境信息"]).join("、"); color: page.capability.available ? "#1f8a55" : "#c2453d"; Layout.fillWidth: true; wrapMode: Text.Wrap }
    ProgressBar { Layout.fillWidth: true; value: Number(page.status.processed_frames || 0)/Math.max(1,Number(page.status.total_frames || 0)); indeterminate: page.status.state==="running" && !page.status.total_frames }
    ResultPanel { value: ({state:page.status.state,error:page.status.error,processed_frames:page.status.processed_frames,total_frames:page.status.total_frames,detected_frames:page.status.detected_frames}) }
    Label { text: (page.status.log_tail || []).join("\n"); textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true; visible: text.length>0; color: "#776d5a" }
    RowLayout {
        Layout.fillWidth: true
        ComboBox { id: results; property string chosenUrl: ""; onActivated: chosenUrl=(model[currentIndex] || {}).result_url || ""; onModelChanged: {for(var i=0;i<count;++i)if(model[i].result_url===chosenUrl){currentIndex=i;return}} Layout.fillWidth: true; model: page.status.results || []; textRole: "bag_name" }
        Button { text: "查看预览视频"; enabled: results.count>0 && !!(results.model[results.currentIndex] || {}).preview_ready; onClicked: {page.previewUrl=results.model[results.currentIndex].preview_url;preview.open()} }
        Button { objectName: "handpose-load"; text: "载入结果"; enabled: results.count>0 && !workspace.busy.handFrames; onClicked: page.load(results.model[results.currentIndex].result_url) }
    }
    NativePlot { id: plot; Layout.fillWidth: true; lines: page.lines(); referenceBounds: page.referenceBounds }
    RowLayout {
        Layout.fillWidth: true
        Button { objectName: "handpose-play"; text: page.playing ? "暂停" : "播放"; enabled: page.frames.length>0; onClicked: { if(page.index>=page.frames.length-1)page.seek(Number(page.frames[0].t || 0));page.lastTick=Date.now();page.playing=!page.playing } }
        Slider { id: timeline; Layout.fillWidth: true; from: page.frames.length ? Number(page.frames[0].t || 0) : 0; to: page.frames.length ? Math.max(from+1,Number(page.frames[page.frames.length-1].t || 0)) : 1; value: page.timeMs; enabled: page.frames.length>0; onMoved: page.seek(value) }
        Label { text: (page.timeMs/1000).toFixed(2)+" s · "+page.index+" / "+page.frames.length }
    }
    Timer { interval: 33; repeat: true; running: page.playing && page.visible; onTriggered: {var now=Date.now();page.seek(page.timeMs+now-page.lastTick);page.lastTick=now;if(page.index>=page.frames.length-1)page.playing=false} }
    Dialog {
        id: preview; title: "手部姿态预览"; modal: true; width: Math.min(900,page.width); height: 600; anchors.centerIn: Overlay.overlay
        standardButtons: Dialog.Close
        Loader {
            anchors.fill: parent; active: preview.visible
            sourceComponent: Component {
                NativeVideo { cameraName: "hand-preview"; endpoint: workspace.resolve(page.previewUrl); media: endpoint; active: preview.visible; visible: preview.visible }
            }
        }
    }
    Connections {
        target: workspace
        function onCompleted(key,success) {
            if(key==="handFrames" && success) { page.frames=Tracking.stabilizeHandedness(workspace.data.handFrames || []);var min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity],count=0;page.frames.forEach(function(f){(f.h || []).forEach(function(h){for(var i=0;i+2<(h.p || []).length;i+=3){var p=[h.p[i],-h.p[i+1],-h.p[i+2]];for(var j=0;j<3;++j){min[j]=Math.min(min[j],p[j]);max[j]=Math.max(max[j],p[j])}count++}})});page.referenceBounds=count ? {min:min,max:max} : null;page.seek(page.frames.length ? Number(page.frames[0].t || 0) : 0);plot.reset() }
            if(key==="handpose" && success && page.status.state==="done" && page.status.result_ready && page.status.result_url!==page.loadedUrl)page.load(page.status.result_url)
        }
    }
}
