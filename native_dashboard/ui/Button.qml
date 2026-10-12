import QtQuick
import QtQuick.Controls as C

C.Button {
    id: control
    property string tone: highlighted ? "primary" : "neutral"
    property bool danger: tone === "danger"
    implicitHeight: 42
    implicitWidth: Math.max(84, contentItem.implicitWidth + 32)
    leftPadding: 16
    rightPadding: 16
    hoverEnabled: true
    font.family: Theme.bodyFont
    font.pixelSize: 13
    font.weight: Font.DemiBold
    opacity: enabled ? 1 : 0.46
    contentItem: Text {
        text: control.text
        font: control.font
        color: control.danger ? "#ffffff" : Theme.ink
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        radius: 8
        color: control.danger ? (control.down ? "#a2342e" : control.hovered ? "#d55e57" : Theme.red) : control.tone === "primary" ? (control.down ? "#ec9a09" : control.hovered ? "#ffc14d" : Theme.amber) : control.down ? Theme.accentSoft : control.hovered ? "#ffffff" : Theme.surface
        border.width: control.activeFocus ? 2 : 1
        border.color: control.activeFocus || control.hovered ? Theme.accent : control.danger ? Theme.red : control.tone === "primary" ? "#d59219" : Theme.lineStrong
    }
}
