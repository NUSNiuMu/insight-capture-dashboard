pragma Singleton
import QtQuick

QtObject {
    readonly property color paper: "#f6f0e4"
    readonly property color surface: "#fffcf5"
    readonly property color soft: "#faf6ec"
    readonly property color sidebar: "#efe8d8"
    readonly property color ink: "#2a2318"
    readonly property color secondary: "#4a4132"
    readonly property color muted: "#7a7060"
    readonly property color faint: "#a39885"
    readonly property color line: "#e3dac6"
    readonly property color lineStrong: "#cfc4aa"
    readonly property color accent: "#b87700"
    readonly property color amber: "#ffb020"
    readonly property color accentSoft: "#f8ecd2"
    readonly property color teal: "#0e7490"
    readonly property color green: "#1f8a55"
    readonly property color red: "#c2453e"
    readonly property string bodyFont: inter.status === FontLoader.Ready ? inter.name : "Noto Sans CJK SC"
    readonly property string monoFont: "DejaVu Sans Mono"
    property FontLoader inter: FontLoader {
        source: Qt.resolvedUrl("fonts/InterVariable.ttf")
    }
}
