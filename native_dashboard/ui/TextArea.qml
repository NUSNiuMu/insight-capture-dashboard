import QtQuick
import QtQuick.Controls as C

C.TextArea {
    id: control
    padding: 13
    color: Theme.ink
    placeholderTextColor: Theme.faint
    font.family: Theme.bodyFont
    font.pixelSize: 14
    selectionColor: Theme.amber
    selectedTextColor: Theme.ink
    background: Rectangle {
        radius: 8
        color: Theme.surface
        border.color: control.activeFocus ? Theme.accent : Theme.lineStrong
        border.width: control.activeFocus ? 2 : 1
    }
}
