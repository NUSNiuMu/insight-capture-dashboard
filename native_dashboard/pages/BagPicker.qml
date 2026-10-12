import QtQuick
import QtQuick.Controls
ComboBox {
    id: picker
    model: ((workspace.data.bags || {}).bags || [])
    textRole: "name"
    valueRole: "id"
    property string selector: currentIndex >= 0 ? String(model[currentIndex].id || model[currentIndex].name) : ""
    property string chosen: ""
    onActivated: chosen = selector
    onModelChanged: {
        for (var i=0;i<count;++i) if (String(model[i].id || model[i].name) === chosen) { currentIndex=i; return }
    }
    displayText: count ? currentText : "暂无录制"
}
