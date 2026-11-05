import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

Ui.Panel {
    id: panel
    property var value: ({})
    property string heading: "Results"
    property var labels: ({
            "session": "Current task",
            "stats": "Statistics",
            "active": "Active",
            "state": "Status",
            "status": "Status",
            "error": "Error",
            "output_path": "Output path",
            "bag_name": "Recording",
            "task_name": "Task",
            "score": "Score",
            "quality": "Quality",
            "completed_bags": "Completed recordings",
            "bag_count": "Recordings",
            "total_frames": "Total frames",
            "processed_frames": "Processed frames",
            "detected_frames": "Detected frames",
            "step_name": "Current step",
            "name": "Name",
            "topic": "Topic",
            "ok": "Pass",
            "loss_pct": "Loss %",
            "msgs": "Messages",
            "avg_hz": "Measured Hz",
            "nominal_hz": "Nominal Hz",
            "recording": "Recording",
            "available": "Available",
            "missing": "Missing dependencies",
            "usable_fraction": "Usable",
            "duration_s": "Duration (s)",
            "episodes": "Episodes",
            "frames": "Frames",
            "size_label": "Size",
            "relative_path": "Folder",
            "stage": "Stage",
            "current_bag": "Current recording",
            "recorded_takes": "Takes",
            "next_take_id": "Next take",
            "session_id": "Task ID",
            "instruction": "Instructions",
            "operator_decision": "Decision",
            "reject_reason": "Rejection reason",
            "integrity": "Integrity",
            "integrity_report": "Integrity report",
            "topics": "Topic",
            "cameras": "Camera",
            "results": "Results",
            "result": "Result",
            "storage": "Storage",
            "capture_profile": "Capture profile",
            "root": "Root",
            "path": "Path",
            "method": "Method",
            "recording_subdirectory": "Recording subfolder",
            "run_name": "Run name", "step": "Step", "sub_progress": "Progress",
            "started_at": "Started", "finished_at": "Finished", "pid": "PID",
            "elapsed_s": "Elapsed (s)", "returncode": "Exit code", "has_sim3": "Trajectory aligned"
        })
    function rows(v, prefix, out, depth) {
        if (depth > 5 || v === null || v === undefined)
            return;
        if (typeof v !== "object") {
            out.push({
                    "label": prefix,
                    "value": typeof v === "boolean" ? (v ? "Yes" : "No") : Ui.Texts.value(v)
                });
            return;
        }
        if (Array.isArray(v)) {
            for (var i = 0; i < v.length; ++i) {
                var item = v[i];
                var title = typeof item === "object" && item ? (item.name || item.topic || item.bag_name || "Entry " + (i + 1)) : String(i + 1);
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
            text: "No results"
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
