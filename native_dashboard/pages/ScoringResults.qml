import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

GridLayout {
    id: results
    property var result: null
    property var cameras: !result ? [] : Array.isArray(result.cameras) ? result.cameras : [result]
    Layout.fillWidth: true
    columns: width >= 1000 ? 3 : width >= 660 ? 2 : 1
    rowSpacing: 14
    columnSpacing: 14
    Repeater {
        model: results.cameras
        delegate: Ui.Section {
            id: card
            required property var modelData
            Layout.alignment: Qt.AlignTop
            Label {
                text: card.modelData.topic || "轨迹评分"
                textFormat: Text.PlainText
                font.family: Ui.Theme.monoFont
                font.pixelSize: 12
                color: Ui.Theme.muted
                Layout.fillWidth: true
                wrapMode: Text.WrapAnywhere
            }
            function number(value, digits) {
                return value === undefined || value === null || !isFinite(Number(value)) ? "—" : Number(value).toFixed(digits);
            }
            function trace(value) {
                return value === undefined || value === null || !isFinite(Number(value)) ? "—" : Number(value).toExponential(3);
            }
            property var metrics: {
                var c = modelData, out = [];
                if (c.error)
                    return out;
                if (c.base_score !== undefined) {
                    out.push({label: "基础分", value: number(c.base_score, 2)});
                    out.push({label: "短暂异常", value: (c.transient_run_count || 0) + " 次 · 扣 " + number(c.transient_penalty || 0, 2) + " 分"});
                    out.push({label: "持续异常", value: (c.bad_run_count || 0) + " 段 / " + number(c.bad_run_seconds || 0, 2) + " 秒 · 扣 " + number(c.sustained_penalty || 0, 2) + " 分"});
                    out.push({label: "可用比例", value: number(100 * (c.usable_fraction !== undefined ? c.usable_fraction : 1), 1) + "%"});
                }
                if (c.mean_trace !== undefined)
                    out.push({label: "平均协方差 trace", value: trace(c.mean_trace)});
                if (c.p50_trace !== undefined)
                    out.push({label: "p50 trace", value: trace(c.p50_trace)});
                out.push({label: "p90 trace", value: trace(c.p90_trace)});
                out.push({label: "最大 trace", value: trace(c.max_trace)});
                if (c.p99_trace !== undefined)
                    out.push({label: "p99 trace", value: trace(c.p99_trace)});
                return out;
            }
            RowLayout {
                Layout.fillWidth: true
                visible: !card.modelData.error
                Label {
                    text: card.number(card.modelData.score, 2) + " / 100"
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    color: Number(card.modelData.score) >= 90 ? Ui.Theme.green : Number(card.modelData.score) >= 70 ? Ui.Theme.teal : Number(card.modelData.score) >= 50 ? Ui.Theme.accent : Ui.Theme.red
                }
                Label {
                    text: card.modelData.quality || ""
                    color: Ui.Theme.muted
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                }
            }
            Label {
                visible: !!card.modelData.error || !!card.modelData.episode_capped
                text: card.modelData.error || "持续异常超过 1 秒，评分上限为 40 分"
                textFormat: Text.PlainText
                color: Ui.Theme.red
                Layout.fillWidth: true
                wrapMode: Text.WrapAnywhere
            }
            Repeater {
                model: card.metrics
                delegate: RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Label { text: modelData.label; color: Ui.Theme.muted; font.pixelSize: 13; Layout.preferredWidth: Math.min(180, card.width * 0.35); wrapMode: Text.Wrap }
                    Label { text: modelData.value; color: Ui.Theme.secondary; font.pixelSize: 13; Layout.fillWidth: true; wrapMode: Text.Wrap }
                }
            }
        }
    }
}
