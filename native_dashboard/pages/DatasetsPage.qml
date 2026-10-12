import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
PageFrame {
    id: page; property var shell; property var selected: []
    property var status: workspace.data.export || {}
    property bool locked: status.status==="running" || !!workspace.busy.action
    heading: "Datasets · 数据集"
    description: "选择录制，导出 UMI 或 LeRobot 数据集。"
    function toggle(id,checked) { var a=selected.slice();var i=a.indexOf(id);if(checked && i<0)a.push(id);if(!checked && i>=0)a.splice(i,1);selected=a }
    RowLayout {
        Button { text: "全选"; enabled: !page.locked; onClicked: page.selected=((workspace.data.bags || {}).bags || []).filter(function(b){return !b.name.startsWith("fail_")}).map(function(b){return String(b.id || b.name)}) }
        Button { text: "清空"; enabled: !page.locked; onClicked: page.selected=[] }
        Button { text: "刷新"; onClicked: shell.refresh() }
        Label { text: "已选 "+page.selected.length+" 条" }
    }
    Repeater {
        model: (workspace.data.bags || {}).bags || []
        delegate: CheckBox {
            required property var modelData; objectName: "dataset-bag-"+String(modelData.id || modelData.name); Layout.fillWidth: true
            visible: !modelData.name.startsWith("fail_")
            text: modelData.name+" · "+(modelData.size_label || "--")
            checked: page.selected.indexOf(String(modelData.id || modelData.name))>=0
            enabled: !page.locked
            onToggled: page.toggle(String(modelData.id || modelData.name),checked)
        }
    }
    GridLayout {
        columns: 2; Layout.fillWidth: true; enabled: !page.locked
        Label { text: "格式" }
        ComboBox { id: format; objectName: "dataset-format"; model: ["lerobot","umi"]; Layout.fillWidth: true }
        Label { text: "相机布局" }
        ComboBox { id: layout; currentIndex: 2; model: ["单臂 · Insight3 A","单臂 · Insight3 B","双臂 · A + B + 头部"]; Layout.fillWidth: true }
        Label { text: "图像" }
        ComboBox { id: image; model: ["original","224","384"]; Layout.fillWidth: true }
        Label { text: "片段" }
        ComboBox { id: episode; model: ["整条录制","按夹爪停顿自动分段"]; Layout.fillWidth: true }
        Label { text: "任务描述"; visible: format.currentText==="lerobot" }
        TextField { id: task; objectName: "dataset-task"; visible: format.currentText==="lerobot"; Layout.fillWidth: true; placeholderText: "LeRobot 任务描述" }
    }
    Label { text: layout.currentIndex===2 ? "左手：Insight3 B · 右手：Insight3 A · 全局：insight9_a" : "右手："+(layout.currentIndex===0 ? "Insight3 A" : "Insight3 B"); color: "#776d5a"; Layout.fillWidth: true; wrapMode: Text.Wrap }
    Button {
        objectName: "dataset-start"; text: "构建数据集"; highlighted: true
        enabled: !page.locked && page.selected.length>0 && (format.currentText!=="lerobot" || task.text.trim().length>0) && !dashboard.state.recording
        onClicked: shell.mutate("/api/umi-export/start",{bag_names:page.selected,image_mode:image.currentText,episode_mode:episode.currentIndex===0 ? "bag" : "auto_pause",camera_names:layout.currentIndex===0 ? ["insight3_a"] : layout.currentIndex===1 ? ["insight3_b"] : ["insight3_a","insight3_b","insight9_a"],export_format:format.currentText,task:task.text.trim()})
    }
    ProgressBar { Layout.fillWidth: true; indeterminate: page.status.status==="running" && !page.status.bag_count; value: page.status.status==="done" ? 1 : Number(page.status.completed_bags || 0)/Math.max(1,Number(page.status.bag_count || 0)) }
    ResultPanel { heading: "导出进度与结果"; value: page.status }
}
