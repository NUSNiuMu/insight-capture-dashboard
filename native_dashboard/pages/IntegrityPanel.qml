import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

Ui.Section {
    id: panel
    property var report: null
    property bool checking: false
    property string error: ""
    property var topics: report && Array.isArray(report.topics) ? report.topics : []
    heading: "Integrity report"
    visible: checking || !!error || !!report

    function number(value, digits) {
        return value === null || value === undefined || !isFinite(Number(value)) ? "—" : Number(value).toLocaleString(Qt.locale(), 'f', digits);
    }
    function detail(topic) {
        if (topic.ok && topic.audit === "latched_static_presence")
            return "Static topic · Recorded";
        if (topic.ok && topic.audit === "source_silent")
            return "Event topic · No messages";
        if (topic.ok && (topic.nominal_hz === null || topic.nominal_hz === undefined))
            return "Message presence only";
        if (topic.error)
            return topic.error;
        return number(topic.avg_hz, 2) + " / " + number(topic.nominal_hz, 0) + " Hz · Loss " + number(topic.loss_pct, 2) + "%";
    }
    RowLayout {
        Layout.fillWidth: true
        BusyIndicator {
            running: panel.checking
            visible: running
            implicitWidth: 28
            implicitHeight: 28
        }
        Ui.Badge {
            visible: !panel.checking && !!panel.report && !panel.error
            text: panel.report && panel.report.ok ? "Complete" : "Incomplete"
            tone: panel.report && panel.report.ok ? "success" : "danger"
        }
        Label {
            Layout.fillWidth: true
            text: panel.checking ? "Checking recording…" : panel.error ? panel.error : panel.report ? (panel.report.bag || panel.report.bag_name || "") : ""
            textFormat: Text.PlainText
            wrapMode: Text.WrapAnywhere
            color: panel.error ? Ui.Theme.red : Ui.Theme.ink
            font.pixelSize: 14
        }
    }
    Label {
        Layout.fillWidth: true
        visible: !!panel.report && !panel.checking && !panel.error
        text: !panel.report ? "" : panel.report.ok
              ? (panel.report.scope === "image_streams" ? "Image streams" : "Topics") + "Passed" + (panel.report.max_loss_pct !== undefined ? " · Loss limit " + panel.number(panel.report.max_loss_pct, 2) + "%" : "")
              : "Failed: " + ((panel.report.failed_topics || []).join("、") || "See topic results")
        textFormat: Text.PlainText
        wrapMode: Text.WrapAnywhere
        color: Ui.Theme.muted
        font.pixelSize: 13
    }
    ColumnLayout {
        Layout.fillWidth: true
        spacing: 0
        visible: !panel.checking && !panel.error && panel.topics.length > 0
        RowLayout {
            Layout.fillWidth: true
            Layout.bottomMargin: 8
            Label { text: "Topic"; color: Ui.Theme.muted; Layout.preferredWidth: (panel.width - panel.leftPadding - panel.rightPadding) * 0.38 }
            Label { text: "Result"; color: Ui.Theme.muted; Layout.preferredWidth: 70 }
            Label { text: "Messages and quality"; color: Ui.Theme.muted; Layout.fillWidth: true }
        }
        Repeater {
            model: panel.topics
            delegate: ColumnLayout {
                required property var modelData
                Layout.fillWidth: true
                spacing: 0
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Ui.Theme.line }
                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 10
                    Layout.bottomMargin: 10
                    Label {
                        text: modelData.name || modelData.topic || "—"
                        textFormat: Text.PlainText
                        Layout.preferredWidth: (panel.width - panel.leftPadding - panel.rightPadding) * 0.38
                        wrapMode: Text.WrapAnywhere
                        font.family: Ui.Theme.monoFont
                        font.pixelSize: 12
                        color: Ui.Theme.ink
                    }
                    Ui.Badge {
                        Layout.preferredWidth: 70
                        text: !modelData.ok ? "Failed" : modelData.audit === "source_silent" ? "No messages" : modelData.nominal_hz === undefined || modelData.nominal_hz === null ? "Recorded" : "Pass"
                        tone: !modelData.ok ? "danger" : modelData.audit === "source_silent" ? "neutral" : "success"
                    }
                    Label {
                        text: panel.number(modelData.msgs, 0) + " messages · " + panel.detail(modelData)
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        wrapMode: Text.WrapAnywhere
                        color: modelData.ok ? Ui.Theme.muted : Ui.Theme.red
                        font.pixelSize: 13
                    }
                }
            }
        }
    }
}
