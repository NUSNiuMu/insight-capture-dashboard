import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Rectangle {
    id: sidebar
    required property string currentPage
    property bool compact: false
    signal selected(string page)
    property var links: [
        {page:"sessions",name:"Sessions",group:"Review"},
        {page:"spatial",name:"Spatial review",group:""},
        {page:"bags",name:"Rosbags",group:""},
        {page:"datasets",name:"Datasets",group:"Process"},
        {page:"scoring",name:"Scoring",group:""},
        {page:"handpose",name:"Hand pose",group:"Advanced"},
        {page:"optimization",name:"Optimization",group:"Engineering"},
        {page:"recording",name:"Recorder",group:"Maintenance"},
        {page:"settings",name:"Settings",group:""}]
    color: "#efe8d8"
    ColumnLayout {
        anchors.fill: parent; anchors.margins: sidebar.compact ? 8 : 14; spacing: 8
        Label { text: sidebar.compact ? "I" : "INSIGHT"; font.bold: true; font.pixelSize: 25; color: "#b87813"; Layout.alignment: Qt.AlignHCenter }
        ScrollView {
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            Column {
                width: parent.width; spacing: 5
                Repeater {
                    id: navRepeater; model: sidebar.links
                    delegate: Column {
                        required property var modelData; required property int index
                        width: parent.width
                        Label { text: modelData.group; visible: text.length>0 && !sidebar.compact; height: visible ? 31 : 0; topPadding: 12; color: "#84775f"; font.pixelSize: 11 }
                        Button {
                            objectName: "nav-"+modelData.page
                            width: parent.width; height: 45
                            text: (index+1<10 ? "0" : "")+(index+1)+(sidebar.compact ? "" : "  "+modelData.name)
                            highlighted: sidebar.currentPage === modelData.page
                            onClicked: sidebar.selected(modelData.page)
                            ToolTip.visible: hovered; ToolTip.text: modelData.name
                            contentItem: Label { text: parent.text; verticalAlignment: Text.AlignVCenter; color: sidebar.currentPage===modelData.page ? "#a36508" : "#494438"; font.bold: sidebar.currentPage===modelData.page; elide: Text.ElideRight }
                            background: Rectangle { color: sidebar.currentPage===modelData.page ? "#e5d7b9" : parent.hovered ? "#f4eede" : "transparent"; radius: 6 }
                        }
                    }
                }
            }
        }
        Label { text: dashboard.connected ? (sidebar.compact ? "●" : "● 后端已连接") : (sidebar.compact ? "○" : "○ 后端未连接"); color: dashboard.connected ? "#1f8a55" : "#c2453d"; Layout.fillWidth: true }
        Label { text: "● 录制中"; visible: !!dashboard.state.recording; color: "#c2453d"; font.bold: true }
    }
}
