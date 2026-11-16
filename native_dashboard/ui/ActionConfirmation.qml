import QtQuick
import QtQuick.Controls as C

Dialog {
    id: confirmation
    required property var controller
    property string action: ""
    property string value: ""
    property var takeIdentity: ({})
    modal: true
    width: 400
    standardButtons: C.Dialog.Ok | C.Dialog.Cancel

    function showFor(action, value, title, take) {
        confirmation.action = action;
        confirmation.value = value;
        confirmation.title = title;
        // Copy primitives now; a remote viewer may change the displayed Take.
        takeIdentity = {
            task_id: take.task_id || "",
            take_id: take.take_id || 0,
            task_name: take.task_name || take.task_id || ""
        };
        open();
    }
    onAccepted: controller.command(action, value, takeIdentity)
    C.Label {
        text: confirmation.action === "reject"
            ? confirmation.takeIdentity.task_name + " · Take " + confirmation.takeIdentity.take_id
            : "Apply to the current device."
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        padding: 16
    }
}
