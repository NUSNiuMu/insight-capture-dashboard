import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ScrollView {
    id: frame
    required property string heading
    property string description: ""
    default property alias content: body.data
    clip: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    ColumnLayout {
        id: body; width: frame.availableWidth; spacing: 16
        Label { text: frame.heading; font.pixelSize: 27; font.bold: true; color: "#25251f"; Layout.fillWidth: true }
        Label { text: frame.description; visible: text.length > 0; wrapMode: Text.Wrap; color: "#776d5a"; Layout.fillWidth: true }
    }
}
