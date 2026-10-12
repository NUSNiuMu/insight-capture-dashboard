import QtQuick
import QtQuick.Controls as C
import QtQuick.Layouts

C.Dialog {
    id: dialog
    padding: 24
    font.family: Theme.bodyFont
    font.pixelSize: 14
    background: Rectangle {
        color: Theme.surface
        radius: 16
        border.color: Theme.lineStrong
    }
    header: C.Label {
        text: dialog.title
        color: Theme.ink
        font.family: Theme.bodyFont
        font.pixelSize: 21
        font.weight: Font.DemiBold
        leftPadding: 24
        rightPadding: 24
        topPadding: 24
        bottomPadding: 8
        elide: Text.ElideRight
    }
    footer: C.DialogButtonBox {
        visible: dialog.standardButtons !== C.Dialog.NoButton
        standardButtons: dialog.standardButtons
        padding: 18
        delegate: Button {
            tone: C.DialogButtonBox.buttonRole === C.DialogButtonBox.AcceptRole ? "primary" : "neutral"
        }
        background: Rectangle {
            color: "transparent"
        }
    }
    C.Overlay.modal: Rectangle {
        color: "#662a2318"
    }
}
