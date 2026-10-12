import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Panel {
    id: card
    property string label: ""
    property string value: "—"
    property string detail: ""
    padding: 20
    contentItem: ColumnLayout {
        spacing: 8
        Label {
            text: card.label
            color: Theme.muted
            font.pixelSize: 12
        }
        Label {
            text: card.value
            color: Theme.teal
            font.family: Theme.monoFont
            font.pixelSize: 28
            font.weight: Font.DemiBold
            Layout.fillWidth: true
            elide: Text.ElideRight
        }
        Label {
            text: card.detail
            visible: text.length > 0
            color: Theme.faint
            font.pixelSize: 11
        }
    }
}
