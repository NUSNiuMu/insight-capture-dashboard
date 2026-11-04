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
    heading: "Scoring"
    description: "Check video integrity and score trajectories."
    Ui.Section {
        eyebrow: "01  QUALITY"
        heading: "Recording"
        RowLayout {
            Layout.fillWidth: true
            BagPicker {
                id: bag
                Layout.fillWidth: true
                enabled: !page.running
            }
            Ui.TextField {
                id: topic
                placeholderText: "Trajectory topic (optional)"
                Layout.preferredWidth: 280
                enabled: !page.running
            }
            Ui.Button {
                text: "Refresh"
                onClicked: shell.refresh()
            }
            Ui.Button {
                objectName: "scoring-run"
                text: "Check and score"
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
        heading: "Scores"
        Layout.fillWidth: true
        Label {
            text: workspace.errors.scoringStart || workspace.errors.scoring || (page.scoring.status === "error" ? page.scoring.error || "Scoring failed" : page.running ? "Scoring…" : page.awaitingResult ? "Loading scores…" : page.scoring.status === "done" ? "Scored: " + (page.scoring.bag_name || "") : "Select a recording to score.")
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
