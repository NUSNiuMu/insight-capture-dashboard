import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Panel {
    id: section
    property string heading: ""
    property string eyebrow: ""
    property string description: ""
    default property alias content: body.data
    Layout.fillWidth: true
    contentItem: ColumnLayout {
        id: body
        spacing: 16
        Label {
            text: section.eyebrow
            visible: text.length > 0
            color: Theme.accent
            font.family: Theme.monoFont
            font.pixelSize: 11
            font.weight: Font.DemiBold
            font.letterSpacing: 1.1
        }
        Label {
            text: section.heading
            visible: text.length > 0
            color: Theme.ink
            font.family: Theme.bodyFont
            font.pixelSize: 19
            font.weight: Font.DemiBold
            Layout.fillWidth: true
            wrapMode: Text.Wrap
        }
        Label {
            text: section.description
            visible: text.length > 0
            color: Theme.muted
            font.pixelSize: 13
            Layout.fillWidth: true
            wrapMode: Text.Wrap
        }
    }
}
