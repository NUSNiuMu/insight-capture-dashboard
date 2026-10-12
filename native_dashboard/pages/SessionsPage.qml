import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
PageFrame {
    id: page; property var shell
    heading: "Sessions · 任务"
    description: "创建和管理任务，查看采集记录与质量检查。"
    property var taskData: workspace.data.tasks || {}
    property var sessionData: workspace.data.sessions || {}
    property var selected: taskSelect.currentIndex>=0 ? (taskData.tasks || [])[taskSelect.currentIndex] : null
    property bool locked: !!dashboard.state.recording || !!workspace.busy.action
    property string editing: ""
    function edit(task) {
        editing=task ? task.task_id : ""; taskId.text=editing; taskName.text=task ? task.name : ""
        speech.text=task ? task.speech_name || "" : ""; instruction.text=task ? task.instruction || "" : ""
        profile.text=task ? task.capture_profile || "dual_arm_umi" : "dual_arm_umi"; editor.open()
    }
    RowLayout {
        Layout.fillWidth: true
        ComboBox { id: taskSelect; objectName: "session-task"; Layout.fillWidth: true; model: page.taskData.tasks || []; textRole: "name"; enabled: !page.locked }
        Button { objectName: "session-enter"; text: "进入任务"; enabled: !page.locked && !!page.selected && page.taskData.active_task_id!==page.selected.task_id; onClicked: shell.mutate("/api/tasks/"+encodeURIComponent(page.selected.task_id)+"/activate") }
        Button { objectName: "session-new"; text: "新建任务"; enabled: !page.locked; onClicked: page.edit(null) }
        Button { objectName: "session-edit"; text: "编辑"; enabled: !page.locked && !!page.selected; onClicked: page.edit(page.selected) }
        Button { objectName: "session-end"; text: "结束任务"; enabled: !page.locked && !!page.taskData.active_task_id; onClicked: endDialog.open() }
        Button { text: "刷新"; onClicked: shell.refresh() }
    }
    ResultPanel { heading: "任务说明"; value: page.selected ? {instruction:page.selected.instruction,recording_subdirectory:page.selected.recording_subdirectory,capture_profile:page.selected.capture_profile} : {} }
    ResultPanel { heading: "当前任务与统计"; value: ({session:page.sessionData.session || {},stats:page.sessionData.stats || {}}) }
    Label { text: "当前任务暂无录制"; visible: (page.sessionData.takes || []).length===0; color: "#776d5a" }
    Repeater {
        model: page.sessionData.takes || []
        delegate: Frame {
            required property var modelData; Layout.fillWidth: true
            ColumnLayout {
                anchors.fill: parent
                RowLayout {
                    Label { text: "TAKE "+modelData.take_id+"  ·  "+(modelData.bag_name || "等待录制归档"); Layout.fillWidth: true; font.bold: true; wrapMode: Text.Wrap }
                    Label { text: modelData.operator_valid===false ? "已作废" : (modelData.quick_qc || {}).state || "等待检查"; color: modelData.operator_valid===false ? "#c2453d" : "#1f8a55" }
                    Button { text: "空间回放"; enabled: !!modelData.bag_id && !dashboard.state.recording; onClicked: shell.openSpatial(modelData.bag_id) }
                }
                Label { text: (modelData.operator_decision || "待审核")+" · "+(modelData.reject_reason || modelData.state || "")+" · "+(modelData.anomaly_timeline || []).length+" 个异常事件"; Layout.fillWidth: true; wrapMode: Text.Wrap }
            }
        }
    }
    Dialog {
        id: endDialog; width: 440; implicitWidth: 440; title: "确认结束当前任务？"; modal: true; anchors.centerIn: Overlay.overlay; standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: shell.mutate("/api/tasks/current/end")
        Label { text: "已经录制的数据将保留。" }
    }
    Dialog {
        id: editor; objectName: "task-editor"; title: page.editing ? "编辑任务" : "新建任务"; modal: true; width: 480; anchors.centerIn: Overlay.overlay
        ColumnLayout {
            anchors.fill: parent
            Label { text: "任务 ID" }
            TextField { id: taskId; objectName: "task-id"; Layout.fillWidth: true; enabled: !page.editing; placeholderText: "例如 pick_and_place" }
            Label { text: "任务名称" }
            TextField { id: taskName; objectName: "task-name"; Layout.fillWidth: true }
            Label { text: "语音名称" }
            TextField { id: speech; Layout.fillWidth: true }
            Label { text: "采集配置" }
            TextField { id: profile; Layout.fillWidth: true }
            Label { text: "操作说明" }
            TextArea { id: instruction; objectName: "task-instruction"; Layout.fillWidth: true; wrapMode: Text.Wrap; implicitHeight: 100 }
            Label { text: workspace.errors.action || ""; visible: text.length>0; color: "#c2453d"; Layout.fillWidth: true; wrapMode: Text.Wrap }
            RowLayout {
                Button { objectName: "task-save"; text: "保存"; enabled: taskId.text.trim().length>0 && taskName.text.trim().length>0 && instruction.text.trim().length>0 && !workspace.busy.action; onClicked: shell.mutate(page.editing ? "/api/tasks/"+encodeURIComponent(page.editing) : "/api/tasks",{id:taskId.text.trim(),name:taskName.text.trim(),speech_name:speech.text.trim(),capture_profile:profile.text.trim(),instruction:instruction.text.trim()},page.editing ? "PUT" : "POST") }
                Button { text: "取消"; onClicked: editor.close() }
            }
        }
    }
    Connections { target: workspace; function onCompleted(key,success) { if(key==="action" && success)editor.close() } }
}
