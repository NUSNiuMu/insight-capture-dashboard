import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

PageFrame {
    id: page
    property var shell
    property var selected: []
    property var status: workspace.data.export || {}
    property bool locked: status.status === "running" || !!workspace.busy.action
    heading: "Datasets · 数据集"
    description: "选择录制与训练参数，将采集数据整理成 UMI 或 LeRobot 数据集。"
    function toggle(id, checked) {
        var a = selected.slice();
        var i = a.indexOf(id);
        if (checked && i < 0)
            a.push(id);
        if (!checked && i >= 0)
            a.splice(i, 1);
        selected = a;
    }
    GridLayout {
        columns: page.width < 1050 ? 1 : 2
        columnSpacing: 20
        rowSpacing: 20
        Layout.fillWidth: true
        Ui.Section {
            eyebrow: "01  SOURCE RECORDINGS"
            heading: "选择录制"
            description: "选择一条或多条已完成的录制。"
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: 430
            RowLayout {
                Ui.Button {
                    text: "全选"
                    enabled: !page.locked
                    onClicked: page.selected = ((workspace.data.bags || {}).bags || []).filter(function (b) {
                            return !b.name.startsWith("fail_");
                        }).map(function (b) {
                            return String(b.id || b.name);
                        })
                }
                Ui.Button {
                    text: "清空"
                    enabled: !page.locked
                    onClicked: page.selected = []
                }
                Ui.Button {
                    text: "刷新"
                    onClicked: shell.refresh()
                }
            }
            Ui.Badge {
                text: "已选 " + page.selected.length + " 条录制"
                tone: "accent"
            }
            Repeater {
                model: (workspace.data.bags || {}).bags || []
                delegate: Ui.CheckBox {
                    required property var modelData
                    objectName: "dataset-bag-" + String(modelData.id || modelData.name)
                    Layout.fillWidth: true
                    visible: !modelData.name.startsWith("fail_")
                    text: modelData.name + " · " + (modelData.size_label || "--")
                    checked: page.selected.indexOf(String(modelData.id || modelData.name)) >= 0
                    enabled: !page.locked
                    onToggled: page.toggle(String(modelData.id || modelData.name), checked)
                }
            }
            Label {
                text: "录制库中暂无可选录制"
                visible: ((workspace.data.bags || {}).bags || []).length === 0
                color: Ui.Theme.muted
            }
        }
        Ui.Section {
            eyebrow: "02  EXPORT OPTIONS"
            heading: "训练数据配置"
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: 640
            GridLayout {
                columns: 2
                rowSpacing: 16
                columnSpacing: 18
                Layout.fillWidth: true
                enabled: !page.locked
                Label {
                    text: "数据格式"
                    color: Ui.Theme.secondary
                }
                Ui.ComboBox {
                    id: format
                    objectName: "dataset-format"
                    model: ["lerobot", "umi"]
                    Layout.fillWidth: true
                }
                Label {
                    text: "相机布局"
                    color: Ui.Theme.secondary
                }
                Ui.ComboBox {
                    id: layout
                    currentIndex: 2
                    model: ["单臂 · Insight3 A", "单臂 · Insight3 B", "双臂 · A + B + 头部"]
                    Layout.fillWidth: true
                }
                Label {
                    text: "图像尺寸"
                    color: Ui.Theme.secondary
                }
                Ui.ComboBox {
                    id: image
                    model: ["original", "224", "384"]
                    Layout.fillWidth: true
                }
                Label {
                    text: "片段方式"
                    color: Ui.Theme.secondary
                }
                Ui.ComboBox {
                    id: episode
                    model: ["整条录制", "按夹爪停顿自动分段"]
                    Layout.fillWidth: true
                }
                Label {
                    text: "任务描述"
                    visible: format.currentText === "lerobot"
                    color: Ui.Theme.secondary
                }
                Ui.TextField {
                    id: task
                    objectName: "dataset-task"
                    visible: format.currentText === "lerobot"
                    Layout.fillWidth: true
                    placeholderText: "描述机器人需要执行的动作"
                }
            }
            Label {
                text: layout.currentIndex === 2 ? "左手：Insight3 B · 右手：Insight3 A · 全局：insight9_a" : "右手：" + (layout.currentIndex === 0 ? "Insight3 A" : "Insight3 B")
                color: Ui.Theme.muted
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                font.pixelSize: 12
            }
            Ui.Button {
                objectName: "dataset-start"
                text: "构建数据集"
                highlighted: true
                Layout.fillWidth: true
                Layout.topMargin: 8
                enabled: !page.locked && page.selected.length > 0 && (format.currentText !== "lerobot" || task.text.trim().length > 0) && !dashboard.state.recording
                onClicked: shell.mutate("/api/umi-export/start", {
                        "bag_names": page.selected,
                        "image_mode": image.currentText,
                        "episode_mode": episode.currentIndex === 0 ? "bag" : "auto_pause",
                        "camera_names": layout.currentIndex === 0 ? ["insight3_a"] : layout.currentIndex === 1 ? ["insight3_b"] : ["insight3_a", "insight3_b", "insight9_a"],
                        "export_format": format.currentText,
                        "task": task.text.trim()
                    })
            }
        }
    }
    Ui.ProgressBar {
        Layout.fillWidth: true
        indeterminate: page.status.status === "running" && !page.status.bag_count
        value: page.status.status === "done" ? 1 : Number(page.status.completed_bags || 0) / Math.max(1, Number(page.status.bag_count || 0))
    }
    ResultPanel {
        heading: "导出进度与结果"
        value: page.status
    }
}
