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
    heading: "完整性报告"
    visible: checking || !!error || !!report

    function number(value, digits) {
        return value === null || value === undefined || !isFinite(Number(value)) ? "—" : Number(value).toLocaleString(Qt.locale(), 'f', digits);
    }
    function detail(topic) {
        if (topic.ok && topic.audit === "latched_static_presence")
            return "静态话题 · 已记录，不检查帧率";
        if (topic.ok && topic.audit === "source_silent")
            return "条件或事件话题未产生消息，不判断丢帧";
        if (topic.ok && (topic.nominal_hz === null || topic.nominal_hz === undefined))
            return "仅检查消息存在，不判断丢帧";
        if (topic.error)
            return topic.error;
        return number(topic.avg_hz, 2) + " / " + number(topic.nominal_hz, 0) + " Hz · 丢帧 " + number(topic.loss_pct, 2) + "%";
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
            text: panel.report && panel.report.ok ? "完整" : "不完整"
            tone: panel.report && panel.report.ok ? "success" : "danger"
        }
        Label {
            Layout.fillWidth: true
            text: panel.checking ? "正在检查录制…" : panel.error ? panel.error : panel.report ? (panel.report.bag || panel.report.bag_name || "") : ""
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
              ? (panel.report.scope === "image_streams" ? "已录制图像流" : "已录制话题") + "检查通过" + (panel.report.max_loss_pct !== undefined ? " · 丢帧阈值 " + panel.number(panel.report.max_loss_pct, 2) + "%" : "")
              : "未通过：" + ((panel.report.failed_topics || []).join("、") || "请查看各话题结果")
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
            Label { text: "话题"; color: Ui.Theme.muted; Layout.preferredWidth: (panel.width - panel.leftPadding - panel.rightPadding) * 0.38 }
            Label { text: "结果"; color: Ui.Theme.muted; Layout.preferredWidth: 70 }
            Label { text: "消息与采集质量"; color: Ui.Theme.muted; Layout.fillWidth: true }
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
                        text: !modelData.ok ? "失败" : modelData.audit === "source_silent" ? "未产生" : modelData.nominal_hz === undefined || modelData.nominal_hz === null ? "已记录" : "通过"
                        tone: !modelData.ok ? "danger" : modelData.audit === "source_silent" ? "neutral" : "success"
                    }
                    Label {
                        text: panel.number(modelData.msgs, 0) + " 条消息 · " + panel.detail(modelData)
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
