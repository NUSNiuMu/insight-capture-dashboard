import QtQuick

ComboBox {
    property string activeTaskId: ""
    property string selectedTaskId: activeTaskId
    textRole: "name"
    valueRole: "task_id"

    function syncSelection() {
        currentIndex = indexOfValue(selectedTaskId);
    }
    onActiveTaskIdChanged: selectedTaskId = activeTaskId
    onSelectedTaskIdChanged: Qt.callLater(syncSelection)
    onModelChanged: Qt.callLater(syncSelection)
    onActivated: selectedTaskId = currentValue
    Component.onCompleted: syncSelection()
}
