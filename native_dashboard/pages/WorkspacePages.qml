import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Loader {
    id: pages
    required property string page
    property string scope: "all"
    signal openSpatial(string bag)
    property var titles: ({sessions:"Sessions · 任务",bags:"Rosbags · 录制库",datasets:"Datasets · 数据集",scoring:"Scoring · 评分",handpose:"Hand pose · 手部姿态",optimization:"Optimization · 优化",recording:"Recorder · 录制",settings:"Settings · 设置"})
    function get(key,path) { workspace.request(key,path) }
    function mutate(path,body,method) { workspace.request("action",path,method || "POST",body || {}) }
    function refresh() {
        if (page === "sessions") { get("tasks","/api/tasks"); get("sessions","/api/sessions") }
        if (["bags","datasets","scoring","handpose","optimization"].indexOf(page)>=0) get("bags","/api/rosbags?scope="+scope)
        if (page === "datasets") get("export","/api/umi-export/status")
        if (page === "scoring") get("scoring","/api/scoring/status")
        if (page === "handpose") { get("capabilities","/api/handpose/capabilities"); get("handpose","/api/handpose/status") }
        if (page === "optimization") { get("optimization","/api/optimization/status");get("runs","/api/optimization/runs");get("cameras","/api/cameras") }
        if (page === "recording") { get("recording","/api/recording/status");get("topics","/api/recording/topics");get("directories","/api/recording/storage/directories") }
        if (page === "settings") get("settings","/api/settings")
    }
    function poll() {
        var key={datasets:"export",scoring:"scoring",handpose:"handpose",optimization:"optimization",recording:"recording"}[page]
        var path={datasets:"/api/umi-export/status",scoring:"/api/scoring/status",handpose:"/api/handpose/status",optimization:"/api/optimization/status",recording:"/api/recording/status"}[page]
        if (key) get(key,path)
    }
    source: page ? "qrc:/pages/"+({sessions:"SessionsPage.qml",bags:"BagsPage.qml",datasets:"DatasetsPage.qml",scoring:"ScoringPage.qml",handpose:"HandPosePage.qml",optimization:"OptimizationPage.qml",recording:"RecorderPage.qml",settings:"SettingsPage.qml"})[page] : ""
    onLoaded: { item.shell=pages; refresh() }
    Timer { interval: 2000; running: pages.visible; repeat: true; onTriggered: pages.poll() }
    Connections {
        target: workspace
        function onCompleted(key,success) {
            if (key === "action" && success) pages.refresh()
        }
    }
}
