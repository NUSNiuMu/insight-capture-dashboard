import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
PageFrame {
    id: page; property var shell; property var pending: null
    property bool running: (workspace.data.scoring || {}).status==="running" || !!workspace.busy.scoringStart || !!workspace.busy.integrity
    heading: "Scoring · 评分"
    description: "先检查图像流完整性，再运行轨迹评分。"
    RowLayout {
        Layout.fillWidth: true
        BagPicker { id: bag; Layout.fillWidth: true; enabled: !page.running }
        TextField { id: topic; placeholderText: "可选：指定轨迹话题"; Layout.preferredWidth: 280; enabled: !page.running }
        Button { text: "刷新"; onClicked: shell.refresh() }
        Button { objectName: "scoring-run"; text: "检查并评分"; highlighted: true; enabled: !!bag.selector && !page.running && !dashboard.state.recording; onClicked: { page.pending={bag_name:bag.selector}; if(topic.text.trim())page.pending.topic=topic.text.trim();workspace.request("integrity","/api/integrity/run","POST",{bag_name:bag.selector}) } }
    }
    BusyIndicator { running: page.running; visible: running }
    ResultPanel { heading: "完整性报告"; value: workspace.data.integrity || {} }
    ResultPanel { heading: "评分结果"; value: workspace.data.scoring || {} }
    Connections {
        target: workspace
        function onCompleted(key,success) {
            if(key==="integrity" && page.pending) { var body=page.pending;page.pending=null;workspace.request("scoringStart","/api/scoring/run","POST",body) }
            if(key==="scoringStart" && success)shell.poll()
        }
    }
}
