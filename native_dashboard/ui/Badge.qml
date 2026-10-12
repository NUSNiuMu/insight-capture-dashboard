import QtQuick
import QtQuick.Controls

Rectangle {
    id: badge
    property string text: ""
    property string tone: "neutral"
    property color ink: tone === "success" ? Theme.green : tone === "danger" ? Theme.red : tone === "accent" ? Theme.accent : Theme.muted
    implicitWidth: label.implicitWidth + 22
    implicitHeight: 29
    radius: 7
    color: tone === "success" ? "#edf5ed" : tone === "danger" ? "#fbece5" : tone === "accent" ? Theme.accentSoft : Theme.soft
    border.color: tone === "success" ? "#cde2cf" : tone === "danger" ? "#eecfc5" : Theme.line
    Label {
        id: label
        anchors.centerIn: parent
        text: badge.text
        color: badge.ink
        font.family: Theme.bodyFont
        font.pixelSize: 12
        font.weight: Font.Medium
    }
}
