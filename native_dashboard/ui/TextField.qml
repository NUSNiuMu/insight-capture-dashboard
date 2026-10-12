import QtQuick
import QtQuick.Controls as C

C.TextField {
    id: control
    implicitHeight: 44
    implicitWidth: 220
    leftPadding: 13
    rightPadding: 13
    color: Theme.ink
    placeholderTextColor: Theme.faint
    font.family: Theme.bodyFont
    font.pixelSize: 14
    selectionColor: Theme.amber
    selectedTextColor: Theme.ink
    opacity: enabled ? 1 : 0.55
    background: Rectangle {
        radius: 8
        color: control.enabled ? Theme.surface : Theme.soft
        border.color: control.activeFocus ? Theme.accent : Theme.lineStrong
        border.width: control.activeFocus ? 2 : 1
    }
}
