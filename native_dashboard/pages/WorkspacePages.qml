import QtQuick
import QtQuick.Controls

Item {
    id: pages
    required property string page
    property string scope: "all"
    property var cacheStats: ({})
    property var lastSwitch: ({})
    property real switchStarted: 0
    signal openSpatial(string bag)
    property var definitions: [{
            "key": "sessions",
            "file": "SessionsPage.qml"
        }, {
            "key": "bags",
            "file": "BagsPage.qml"
        }, {
            "key": "datasets",
            "file": "DatasetsPage.qml"
        }, {
            "key": "scoring",
            "file": "ScoringPage.qml"
        }, {
            "key": "handpose",
            "file": "HandPosePage.qml"
        }, {
            "key": "optimization",
            "file": "OptimizationPage.qml"
        }, {
            "key": "recording",
            "file": "RecorderPage.qml"
        }, {
            "key": "settings",
            "file": "SettingsPage.qml"
        }]
    property var titles: ({
            "sessions": "Sessions · 任务",
            "bags": "Rosbags · 录制库",
            "datasets": "Datasets · 数据集",
            "scoring": "Scoring · 评分",
            "handpose": "Hand pose · 手部姿态",
            "optimization": "Optimization · 优化",
            "recording": "Recorder · 录制",
            "settings": "Settings · 设置"
        })
    function get(key, path, force) {
        if (force)
            workspace.request(key, path);
        else
            workspace.ensure(key, path, key === "bags" || key === "capabilities" ? 60000 : key === "tasks" || key === "runs" || key === "cameras" ? 15000 : 2000);
    }
    function mutate(path, body, method) {
        workspace.request("action", path, method || "POST", body || {});
    }
    function refresh(force) {
        if (force === undefined)
            force = true;
        if (page === "sessions") {
            get("tasks", "/api/tasks", force);
            get("sessions", "/api/sessions", force);
        }
        if (["bags", "datasets", "scoring", "handpose", "optimization"].indexOf(page) >= 0)
            get("bags", "/api/rosbags?scope=" + scope, force);
        if (page === "datasets")
            get("export", "/api/umi-export/status", force);
        if (page === "scoring")
            get("scoring", "/api/scoring/status", force);
        if (page === "handpose") {
            get("capabilities", "/api/handpose/capabilities", force);
            get("handpose", "/api/handpose/status", force);
        }
        if (page === "optimization") {
            get("optimization", "/api/optimization/status", force);
            get("runs", "/api/optimization/runs", force);
            get("cameras", "/api/cameras", force);
        }
        if (page === "recording") {
            get("recording", "/api/recording/status", force);
            get("topics", "/api/recording/topics", force);
            get("directories", "/api/recording/storage/directories", force);
        }
        if (page === "settings")
            get("settings", "/api/settings", force);
    }
    function poll() {
        var key = {
            "datasets": "export",
            "scoring": "scoring",
            "handpose": "handpose",
            "optimization": "optimization",
            "recording": "recording"
        }[page];
        var path = {
            "datasets": "/api/umi-export/status",
            "scoring": "/api/scoring/status",
            "handpose": "/api/handpose/status",
            "optimization": "/api/optimization/status",
            "recording": "/api/recording/status"
        }[page];
        if (key)
            workspace.request(key, path);
    }
    function ready(key, created) {
        if (created) {
            var stats = Object.assign({}, cacheStats);
            stats[key] = (stats[key] || 0) + 1;
            cacheStats = stats;
        }
        if (page === key) {
            lastSwitch = {
                "page": key,
                "reused": !created,
                "ready_ms": Date.now() - switchStarted
            };
            Qt.callLater(function () {
                    if (pages.page === key)
                        pages.refresh(false);
                });
        }
    }
    function selectPage() {
        switchStarted = Date.now();
        for (var i = 0; i < definitions.length; ++i)
            if (definitions[i].key === page) {
                var loader = pageRepeater.itemAt(i);
                if (!loader)
                    return;
                if (loader.status === Loader.Ready)
                    ready(page, false);
                else
                    loader.loadedOnce = true;
                return;
            }
    }
    onPageChanged: selectPage()
    Component.onCompleted: selectPage()
    Repeater {
        id: pageRepeater
        model: pages.definitions
        delegate: Loader {
            required property var modelData
            property bool loadedOnce: false
            anchors.fill: parent
            visible: pages.visible && pages.page === modelData.key
            active: loadedOnce
            asynchronous: true
            source: "qrc:/pages/" + modelData.file
            onLoaded: {
                item.shell = pages;
                pages.ready(modelData.key, true);
            }
        }
    }
    Label {
        anchors.centerIn: parent
        text: "正在打开页面…"
        color: "#7a7060"
        visible: pages.visible && !!pages.page && !pages.cacheStats[pages.page]
    }
    Timer {
        interval: 2000
        running: pages.visible
        repeat: true
        onTriggered: pages.poll()
    }
    Connections {
        target: workspace
        function onCompleted(key, success) {
            if (key === "action" && success)
                pages.refresh();
        }
    }
}
