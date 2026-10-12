import QtQuick
import QtQuick.Controls as C

C.ProgressBar {
    id: control
    implicitHeight: 8
    implicitWidth: 240
    background: Rectangle {
        implicitHeight: 8
        radius: 4
        color: Theme.line
    }
    contentItem: Item {
        Rectangle {
            width: control.indeterminate ? parent.width * 0.28 : control.position * parent.width
            height: parent.height
            radius: 4
            color: Theme.amber
            SequentialAnimation on opacity  {
                running: control.indeterminate && control.visible
                loops: Animation.Infinite
                NumberAnimation {
                    from: 0.35
                    to: 1
                    duration: 800
                }
                NumberAnimation {
                    from: 1
                    to: 0.35
                    duration: 800
                }
            }
        }
    }
}
