import QtQuick
import QtQuick.Controls as C

C.ComboBox {
    id: control
    implicitHeight: 44
    implicitWidth: 240
    leftPadding: 13
    rightPadding: 36
    font.family: Theme.bodyFont
    font.pixelSize: 14
    hoverEnabled: true
    opacity: enabled ? 1 : 0.55
    contentItem: Text {
        text: control.displayText
        font: control.font
        color: Theme.ink
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    indicator: Canvas {
        width: 12
        height: 8
        x: control.width - 25
        y: (control.height - height) / 2
        onPaint: {
            var ctx = getContext("2d");
            ctx.clearRect(0, 0, width, height);
            ctx.strokeStyle = "#7a7060";
            ctx.lineWidth = 1.7;
            ctx.beginPath();
            ctx.moveTo(1, 1);
            ctx.lineTo(6, 6);
            ctx.lineTo(11, 1);
            ctx.stroke();
        }
    }
    background: Rectangle {
        radius: 8
        color: Theme.surface
        border.color: control.activeFocus || control.hovered ? Theme.accent : Theme.lineStrong
        border.width: control.activeFocus ? 2 : 1
    }
    delegate: C.ItemDelegate {
        width: control.width
        height: 44
        text: control.textRole ? (Array.isArray(control.model) ? modelData[control.textRole] : model[control.textRole]) : modelData
        font: control.font
        highlighted: control.highlightedIndex === index
        contentItem: Text {
            text: parent.text
            font: control.font
            color: Theme.ink
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 5
            color: parent.highlighted || parent.hovered ? Theme.accentSoft : Theme.surface
        }
    }
    popup: C.Popup {
        y: control.height + 6
        width: control.width
        padding: 6
        implicitHeight: Math.min(contentItem.implicitHeight + 12, 360)
        background: Rectangle {
            radius: 10
            color: Theme.surface
            border.color: Theme.lineStrong
        }
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            C.ScrollIndicator.vertical: C.ScrollIndicator {
            }
        }
    }
}
