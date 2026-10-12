import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

ScrollView {
    id: frame
    required property string heading
    property string description: ""
    default property alias content: body.data
    clip: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    ScrollBar.vertical.policy: ScrollBar.AsNeeded
    ColumnLayout {
        width: frame.availableWidth
        Item {
            Layout.preferredHeight: 24
        }
        ColumnLayout {
            id: body
            Layout.fillWidth: true
            Layout.maximumWidth: 1320
            Layout.alignment: Qt.AlignHCenter
            Layout.leftMargin: frame.width < 950 ? 20 : 38
            Layout.rightMargin: frame.width < 950 ? 20 : 38
            spacing: 20
            Label {
                text: "CAPTURE STUDIO  /  " + frame.heading.split(" · ")[0].toUpperCase()
                font.family: Ui.Theme.monoFont
                font.pixelSize: 11
                font.letterSpacing: 1.3
                font.weight: Font.DemiBold
                color: Ui.Theme.accent
            }
            Label {
                text: frame.heading
                font.family: Ui.Theme.bodyFont
                font.pixelSize: frame.width < 950 ? 30 : 36
                font.weight: Font.DemiBold
                color: Ui.Theme.ink
                Layout.fillWidth: true
            }
            Label {
                text: frame.description
                visible: text.length > 0
                wrapMode: Text.Wrap
                color: Ui.Theme.muted
                font.pixelSize: 14
                Layout.fillWidth: true
            }
            Rectangle {
                color: Ui.Theme.line
                Layout.fillWidth: true
                implicitHeight: 1
                Layout.topMargin: 6
                Layout.bottomMargin: 8
            }
        }
        Item {
            Layout.preferredHeight: 34
        }
    }
}
