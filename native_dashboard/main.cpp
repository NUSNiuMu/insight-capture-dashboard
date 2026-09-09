#include "dashboard.h"
#include "video.h"
#include <QCommandLineParser>
#include <QFile>
#include <QGuiApplication>
#include <QJSValue>
#include <QJsonDocument>
#include <QLibrary>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QTimer>
#include <nvbufsurftransform.h>

int main(int argc, char **argv) {
    // R36's transform library otherwise deletes an uninitialized TLS key at exit.
    QLibrary transform("/usr/lib/aarch64-linux-gnu/nvidia/libnvbufsurftransform.so");
    transform.setLoadHints(QLibrary::PreventUnloadHint);
    using SetSession = NvBufSurfTransform_Error (*)(NvBufSurfTransformConfigParams *);
    auto setSession = reinterpret_cast<SetSession>(transform.resolve("NvBufSurfTransformSetSessionParams"));
    NvBufSurfTransformConfigParams session{NvBufSurfTransformCompute_VIC, 0, nullptr};
    if (!setSession || setSession(&session) != NvBufSurfTransformError_Success) {
        qCritical("Cannot initialize NVIDIA transform session");
        return 2;
    }
    gst_init(&argc, &argv);
    QGuiApplication app(argc, argv);
    app.setOrganizationName("Insight");
    app.setApplicationName("NativeDashboard");
    QQuickStyle::setStyle("Basic");
    QQuickWindow::setGraphicsApi(QSGRendererInterface::OpenGL);
    QCommandLineParser args;
    args.setApplicationDescription("Qt Quick / GStreamer / Quick 3D capture dashboard");
    args.addHelpOption();
    args.addOption({"server", "Dashboard base URL", "url", "http://127.0.0.1:8765"});
    args.addOption({"fps", "Preview target", "fps", "25"});
    args.addOption({"fullscreen", "Open full screen"});
    args.addOption({"quit-after", "Exit after N seconds (validation)", "seconds", "0"});
    args.addOption({"diagnostics", "Write local runtime diagnostics", "path"});
    args.process(app);
    QUrl server(args.value("server"));
    if (!server.isValid() || server.host().isEmpty() ||
        !(server.scheme() == "http" || server.scheme() == "https"))
        return 2;
    qmlRegisterType<NativeVideo>("Insight.Native", 1, 0, "NativeVideo");
    qmlRegisterUncreatableType<Pose>("Insight.Native", 1, 0, "Pose", "Created by dashboard");
    qmlRegisterUncreatableType<TrailGeometry>("Insight.Native", 1, 0, "TrailGeometry",
                                              "Created by dashboard");
    Dashboard dashboard(server, qBound(1, args.value("fps").toInt(), 30));
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("dashboard", &dashboard);
    engine.rootContext()->setContextProperty("startFullScreen", args.isSet("fullscreen"));
    engine.load(QUrl("qrc:/Main.qml"));
    if (engine.rootObjects().isEmpty())
        return 3;
    QObject::connect(&app, &QCoreApplication::aboutToQuit, &dashboard, &Dashboard::shutdown);
    const QString path = args.value("diagnostics");
    QTimer diagnosticTimer;
    if (!path.isEmpty()) {
        QObject::connect(&diagnosticTimer, &QTimer::timeout, &app, [&] {
            auto state = dashboard.diagnostics();
            QVariant videos;
            QMetaObject::invokeMethod(engine.rootObjects().first(), "videoDiagnostics",
                                      Q_RETURN_ARG(QVariant, videos));
            state["videos"] = videos.value<QJSValue>().toVariant();
            QVariant spatial;
            QMetaObject::invokeMethod(engine.rootObjects().first(), "spatialDiagnostics",
                                      Q_RETURN_ARG(QVariant, spatial));
            state["spatial"] = spatial.value<QJSValue>().toVariant();
            QVariant controls;
            QMetaObject::invokeMethod(engine.rootObjects().first(), "controlPositions",
                                      Q_RETURN_ARG(QVariant, controls));
            state["controls"] = controls.value<QJSValue>().toVariant();
            state["sceneFps"] = engine.rootObjects().first()->property("sceneFps");
            QFile f(path);
            if (f.open(QIODevice::WriteOnly))
                f.write(QJsonDocument::fromVariant(state).toJson());
        });
        diagnosticTimer.start(1000);
    }
    if (args.value("quit-after").toInt() > 0)
        QTimer::singleShot(args.value("quit-after").toInt() * 1000, &app, &QCoreApplication::quit);
    dashboard.start();
    int result = app.exec();
    gst_deinit();
    return result;
}
