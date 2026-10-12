import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    property var pending: null
    property var integrityReport: null
    property string integrityBag: ""
    property bool awaitingResult: false
    property var scoring: workspace.data.scoring || {}
    property bool running: scoring.status === "running" || !!workspace.busy.scoringStart || !!workspace.busy.scoringIntegrity
    heading: "Scoring · 评分"
    description: "先检查图像流完整性，再运行轨迹评分。"
    Ui.Section {
        eyebrow: "01  QUALITY ASSESSMENT"
        heading: "评分输入"
        RowLayout {
            Layout.fillWidth: true
            BagPicker {
                id: bag
                Layout.fillWidth: true
                enabled: !page.running
            }
            Ui.TextField {
                id: topic
                placeholderText: "可选：指定轨迹话题"
                Layout.preferredWidth: 280
                enabled: !page.running
            }
            Ui.Button {
                text: "刷新"
                onClicked: shell.refresh()
            }
            Ui.Button {
                objectName: "scoring-run"
                text: "检查并评分"
                highlighted: true
                enabled: !!bag.selector && !page.running && !dashboard.state.recording
                onClicked: {
                    page.integrityReport = null;
                    page.integrityBag = bag.selector;
                    page.awaitingResult = true;
                    page.pending = {
                        "bag_name": bag.selector
                    };
                    if (topic.text.trim())
                        page.pending.topic = topic.text.trim();
                    workspace.request("scoringIntegrity", "/api/integrity/run", "POST", {
                            "bag_name": bag.selector
                        });
                }
            }
        }
    }
    BusyIndicator {
        running: page.running
        visible: running
    }
    IntegrityPanel {
        report: page.integrityBag === bag.selector ? page.integrityReport : null
        checking: !!workspace.busy.scoringIntegrity
        error: page.integrityBag === bag.selector ? (workspace.errors.scoringIntegrity || "") : ""
    }
    Ui.Section {
        heading: "评分结果"
        Layout.fillWidth: true
        Label {
            text: workspace.errors.scoringStart || workspace.errors.scoring || (page.scoring.status === "error" ? page.scoring.error || "评分失败" : page.running ? "正在评分…" : page.awaitingResult ? "正在读取评分结果…" : page.scoring.status === "done" ? "已评分：" + (page.scoring.bag_name || "") : "选择录制并运行检查，结果将显示在这里。")
            textFormat: Text.PlainText
            color: workspace.errors.scoringStart || workspace.errors.scoring || page.scoring.status === "error" ? Ui.Theme.red : Ui.Theme.muted
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
        }
        ScoringResults {
            result: !page.awaitingResult && !page.running && page.scoring.status === "done" ? page.scoring.result || null : null
            Layout.fillWidth: true
        }
    }
    Connections {
        target: workspace
        function onCompleted(key, success) {
            if (key === "scoringIntegrity" && page.pending) {
                page.integrityReport = success ? workspace.data.scoringIntegrity : null;
                var body = page.pending;
                page.pending = null;
                workspace.request("scoringStart", "/api/scoring/run", "POST", body);
            }
            if (key === "scoringStart" && success)
                shell.poll();
            if (key === "scoring" && success && !page.pending && !workspace.busy.scoringStart && !workspace.busy.scoringIntegrity)
                page.awaitingResult = false;
        }
    }
}
