import QtQuick
import QtQuick.Controls as C

C.CheckBox {
    id: control
    implicitHeight: 38
    spacing: 11
    leftPadding: 0
    font.family: Theme.bodyFont
    font.pixelSize: 14
    opacity: enabled ? 1 : 0.5
    indicator: Rectangle {
        implicitWidth: 20
        implicitHeight: 20
        x: control.leftPadding
        y: (control.height - height) / 2
        radius: 5
        color: control.checked ? Theme.amber : Theme.surface
        border.color: control.activeFocus ? Theme.accent : control.checked ? Theme.accent : Theme.lineStrong
        border.width: control.activeFocus ? 2 : 1
        Text {
            anchors.centerIn: parent
            text: "✓"
            visible: control.checked
            font.pixelSize: 15
            font.bold: true
            color: Theme.ink
        }
    }
    contentItem: Text {
        text: control.text
        font: control.font
        color: Theme.secondary
        leftPadding: control.indicator.width + control.spacing
        verticalAlignment: Text.AlignVCenter
        wrapMode: Text.Wrap
    }
}
