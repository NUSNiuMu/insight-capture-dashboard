import QtQuick
import QtQuick.Controls as C

C.Slider {
    id: control
    implicitHeight: 40
    implicitWidth: 240
    opacity: enabled ? 1 : 0.5
    background: Rectangle {
        x: control.leftPadding
        y: control.topPadding + (control.availableHeight - height) / 2
        width: control.availableWidth
        height: 5
        radius: 3
        color: Theme.line
        Rectangle {
            width: control.visualPosition * parent.width
            height: parent.height
            radius: 3
            color: Theme.accent
        }
    }
    handle: Rectangle {
        x: control.leftPadding + control.visualPosition * (control.availableWidth - width)
        y: control.topPadding + (control.availableHeight - height) / 2
        width: 19
        height: 19
        radius: 10
        color: Theme.surface
        border.width: 2
        border.color: control.pressed || control.activeFocus ? Theme.accent : Theme.lineStrong
    }
}
