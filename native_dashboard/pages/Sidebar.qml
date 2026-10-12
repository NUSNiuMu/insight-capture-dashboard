import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../ui" as Ui

Rectangle {
    id: sidebar
    required property string currentPage
    property bool compact: false
    signal selected(string page)
    property var links: [{
            "page": "sessions",
            "name": "Sessions",
            "group": "Review"
        }, {
            "page": "spatial",
            "name": "Spatial review",
            "group": ""
        }, {
            "page": "bags",
            "name": "Rosbags",
            "group": ""
        }, {
            "page": "datasets",
            "name": "Datasets",
            "group": "Process"
        }, {
            "page": "scoring",
            "name": "Scoring",
            "group": ""
        }, {
            "page": "handpose",
            "name": "Hand pose",
            "group": "Advanced"
        }, {
            "page": "optimization",
            "name": "Optimization",
            "group": "Engineering"
        }, {
            "page": "recording",
            "name": "Recorder",
            "group": "Maintenance"
        }, {
            "page": "settings",
            "name": "Settings",
            "group": ""
        }]
    color: Ui.Theme.sidebar
    Rectangle {
        anchors.right: parent.right
        height: parent.height
        width: 1
        color: Ui.Theme.lineStrong
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: sidebar.compact ? 10 : 18
        spacing: 14
        RowLayout {
            Layout.fillWidth: true
            Layout.topMargin: 6
            Layout.bottomMargin: 6
            spacing: 12
            Rectangle {
                width: 36
                height: 36
                radius: 9
                color: Ui.Theme.amber
                border.color: Ui.Theme.accent
                Label {
                    anchors.centerIn: parent
                    text: "I"
                    font.family: Ui.Theme.monoFont
                    font.pixelSize: 20
                    font.bold: true
                    color: Ui.Theme.ink
                }
            }
            ColumnLayout {
                visible: !sidebar.compact
                spacing: 3
                Label {
                    text: "INSIGHT"
                    font.family: Ui.Theme.monoFont
                    font.pixelSize: 16
                    font.weight: Font.Bold
                    font.letterSpacing: 2
                    color: Ui.Theme.ink
                }
                Label {
                    text: "Capture Studio"
                    font.pixelSize: 11
                    color: Ui.Theme.muted
                }
            }
        }
        Ui.Panel {
            visible: !sidebar.compact && sidebar.height >= 860
            Layout.fillWidth: true
            padding: 14
            contentItem: ColumnLayout {
                spacing: 7
                Label {
                    text: "LOCAL WORKSPACE"
                    font.family: Ui.Theme.monoFont
                    font.pixelSize: 9
                    font.letterSpacing: .7
                    color: Ui.Theme.muted
                }
                Label {
                    text: "原生采集工作台"
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                    color: Ui.Theme.ink
                }
                Label {
                    text: "采集  ·  检查  ·  导出"
                    font.pixelSize: 11
                    color: Ui.Theme.muted
                }
            }
        }
        ScrollView {
            id: navigation
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            Column {
                width: navigation.availableWidth
                spacing: 4
                Repeater {
                    model: sidebar.links
                    delegate: Column {
                        required property var modelData
                        required property int index
                        width: parent.width
                        Label {
                            text: modelData.group.toUpperCase()
                            visible: text.length > 0 && !sidebar.compact
                            height: visible ? 31 : 0
                            topPadding: 12
                            leftPadding: 10
                            color: Ui.Theme.faint
                            font.family: Ui.Theme.monoFont
                            font.pixelSize: 10
                            font.letterSpacing: 1.1
                        }
                        Button {
                            id: navButton
                            objectName: "nav-" + modelData.page
                            property bool selected: sidebar.currentPage === modelData.page
                            width: parent.width
                            height: sidebar.compact ? 46 : 43
                            Accessible.name: modelData.name
                            onClicked: sidebar.selected(modelData.page)
                            hoverEnabled: true
                            ToolTip.visible: sidebar.compact && hovered
                            ToolTip.delay: 600
                            ToolTip.text: modelData.name
                            background: Rectangle {
                                radius: 8
                                color: navButton.selected ? Ui.Theme.surface : navButton.hovered ? "#f8f3e9" : "transparent"
                                border.color: navButton.selected ? Ui.Theme.lineStrong : "transparent"
                                Rectangle {
                                    width: 3
                                    height: 18
                                    radius: 2
                                    anchors.left: parent.left
                                    anchors.verticalCenter: parent.verticalCenter
                                    color: Ui.Theme.amber
                                    visible: navButton.selected
                                }
                            }
                            contentItem: RowLayout {
                                spacing: 10
                                Rectangle {
                                    width: 27
                                    height: 27
                                    radius: 6
                                    color: navButton.selected ? Ui.Theme.accentSoft : "transparent"
                                    border.color: navButton.selected ? Ui.Theme.accent : Ui.Theme.lineStrong
                                    Label {
                                        anchors.centerIn: parent
                                        text: "0" + (index + 1)
                                        font.family: Ui.Theme.monoFont
                                        font.pixelSize: 10
                                        color: navButton.selected ? Ui.Theme.accent : Ui.Theme.muted
                                    }
                                }
                                Label {
                                    visible: !sidebar.compact
                                    text: modelData.name
                                    font.family: Ui.Theme.bodyFont
                                    font.pixelSize: 13
                                    font.weight: navButton.selected ? Font.DemiBold : Font.Medium
                                    color: Ui.Theme.secondary
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }
                }
            }
        }
        Rectangle {
            color: Ui.Theme.lineStrong
            Layout.fillWidth: true
            implicitHeight: 1
        }
        RowLayout {
            spacing: 10
            Rectangle {
                width: 7
                height: 7
                radius: 4
                color: dashboard.connected ? Ui.Theme.green : Ui.Theme.red
            }
            ColumnLayout {
                visible: !sidebar.compact
                spacing: 4
                Label {
                    text: dashboard.connected ? "后端已连接" : "等待后端连接"
                    font.pixelSize: 12
                    font.weight: Font.DemiBold
                    color: Ui.Theme.secondary
                }
                Label {
                    text: dashboard.state.recording ? "录制进行中" : "本地原生客户端"
                    font.pixelSize: 11
                    color: dashboard.state.recording ? Ui.Theme.red : Ui.Theme.muted
                }
            }
        }
    }
}
