import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

Ui.Panel {
    id: panel
    property var value: ({})
    property var words: ({
            "idle": "待命",
            "running": "进行中",
            "done": "已完成",
            "error": "失败",
            "pass": "通过",
            "suspect": "待复核",
            "pending": "等待处理",
            "good": "良好",
            "yes": "是",
            "no": "否"
        })
    property string heading: "结果与状态"
    property var labels: ({
            "session": "当前任务",
            "stats": "统计",
            "active": "已进入",
            "state": "状态",
            "status": "状态",
            "error": "错误",
            "output_path": "输出路径",
            "bag_name": "录制",
            "task_name": "任务",
            "score": "评分",
            "quality": "质量",
            "completed_bags": "已完成录制",
            "bag_count": "录制数量",
            "total_frames": "总帧数",
            "processed_frames": "已处理帧",
            "detected_frames": "检测帧",
            "step_name": "当前步骤",
            "name": "名称",
            "topic": "话题",
            "ok": "通过",
            "loss_pct": "丢帧率 %",
            "msgs": "消息数",
            "avg_hz": "实际 Hz",
            "nominal_hz": "标称 Hz",
            "recording": "录制中",
            "available": "可用",
            "missing": "缺少依赖",
            "usable_fraction": "可用比例",
            "duration_s": "时长（秒）",
            "episodes": "片段数量",
            "frames": "帧数",
            "size_label": "大小",
            "relative_path": "目录",
            "stage": "阶段",
            "current_bag": "当前录制",
            "recorded_takes": "录制条数",
            "next_take_id": "下一条编号",
            "session_id": "任务 ID",
            "instruction": "操作说明",
            "operator_decision": "人工判断",
            "reject_reason": "作废原因",
            "integrity": "完整性",
            "integrity_report": "完整性报告",
            "topics": "话题",
            "cameras": "相机",
            "results": "导出结果",
            "result": "结果",
            "storage": "存储",
            "voice_audio": "语音",
            "capture_profile": "采集配置",
            "root": "根目录",
            "path": "路径",
            "method": "方法",
            "recording_subdirectory": "录制子目录"
        })
    function rows(v, prefix, out, depth) {
        if (depth > 5 || v === null || v === undefined)
            return;
        if (typeof v !== "object") {
            out.push({
                    "label": prefix,
                    "value": typeof v === "boolean" ? (v ? "是" : "否") : (words[String(v)] || String(v))
                });
            return;
        }
        if (Array.isArray(v)) {
            for (var i = 0; i < v.length; ++i) {
                var item = v[i];
                var title = typeof item === "object" && item ? (item.name || item.topic || item.bag_name || "记录 " + (i + 1)) : String(i + 1);
                rows(item, prefix + " · " + title, out, depth + 1);
            }
            return;
        }
        for (var k in v) {
            if (["log_tail", "positions", "quaternions_xyzw", "trace", "p", "h", "vio", "colmap", "data", "manifest", "frames", "timestamps"].indexOf(k) >= 0 && typeof v[k] === "object")
                continue;
            if (k.endsWith("_url") || k === "colmap_log")
                continue;
            rows(v[k], prefix ? prefix + " · " + (labels[k] || k) : (labels[k] || k), out, depth + 1);
        }
    }
    property var entries: {
        var out = [];
        rows(value, "", out, 0);
        return out;
    }
    Layout.fillWidth: true
    implicitWidth: 400
    implicitHeight: content.implicitHeight + topPadding + bottomPadding
    contentItem: ColumnLayout {
        id: content
        spacing: 12
        Label {
            text: panel.heading
            font.family: Ui.Theme.bodyFont
            font.pixelSize: 19
            font.weight: Font.DemiBold
            color: Ui.Theme.ink
            Layout.bottomMargin: 8
        }
        Label {
            text: "暂无结果"
            visible: panel.entries.length === 0
            color: Ui.Theme.muted
            font.pixelSize: 13
        }
        Repeater {
            model: panel.entries
            delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                Layout.minimumHeight: 27
                Label {
                    text: modelData.label
                    Layout.preferredWidth: Math.min(300, panel.width * 0.38)
                    wrapMode: Text.Wrap
                    color: Ui.Theme.muted
                    font.pixelSize: 13
                }
                Label {
                    text: modelData.value
                    textFormat: Text.PlainText
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    color: Ui.Theme.teal
                    font.pixelSize: 14
                    font.weight: Font.Medium
                }
            }
        }
    }
}
