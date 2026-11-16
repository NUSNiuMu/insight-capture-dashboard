#include "dashboard.h"
#include "workspace.h"
#include <QGuiApplication>
#include <QQmlComponent>
#include <QQmlContext>
#include <QQmlEngine>
#include <QQuickItem>
#include <QQuickWindow>
#include <QJsonDocument>
#include <QTcpServer>
#include <QTcpSocket>
#include <QThread>
#include <cstdio>
#include <memory>

// Exercise real Qt HTTP clients and embedded QML against a dummy loopback peer.
static bool waitFor(const std::function<bool()> &predicate) {
    QElapsedTimer timer;
    timer.start();
    while (!predicate() && timer.elapsed() < 4000) {
        QCoreApplication::processEvents(QEventLoop::AllEvents, 10);
        QThread::msleep(1);
    }
    return predicate();
}
static QObject *named(QObject *root, const QString &name) {
    if (root->objectName() == name) return root;
    if (auto *found = root->findChild<QObject *>(name)) return found;
    if (auto *item = qobject_cast<QQuickItem *>(root))
        for (auto *child : item->childItems())
            if (auto *found = named(child, name)) return found;
    return nullptr;
}
int main(int argc, char **argv) {
    QGuiApplication app(argc, argv);
    QTcpServer server;
    if (!server.listen(QHostAddress::LocalHost)) return 2;
    const QString message = QStringLiteral("当前任务已变化，请重新打开任务列表后操作。");
    QList<QJsonObject> requests;
    QObject::connect(&server, &QTcpServer::newConnection, &app, [&] {
        while (auto *socket = server.nextPendingConnection()) {
            auto buffer = std::make_shared<QByteArray>();
            QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
            QObject::connect(socket, &QTcpSocket::readyRead, socket, [&, socket, buffer] {
                *buffer += socket->readAll();
                const auto split = buffer->indexOf("\r\n\r\n");
                if (split < 0) return;
                const auto headers = buffer->left(split).split('\n');
                int length = 0;
                for (auto line : headers)
                    if (line.toLower().startsWith("content-length:"))
                        length = line.mid(15).trimmed().toInt();
                if (buffer->size() < split + 4 + length) return;
                const auto first = headers.first().split(' ');
                const QByteArray method = first.value(0), path = first.value(1);
                QJsonObject data;
                int status = 200;
                if (method == "POST") {
                    requests.append(QJsonDocument::fromJson(buffer->mid(split + 4, length)).object());
                    data = {{"message", message}};
                    status = 409;
                } else if (path == "/api/recording/status") {
                    data = {{"recording", false}, {"merge_state", "idle"},
                        {"task_status", QJsonObject{{"active", true},
                            {"task", QJsonObject{{"task_id", "task_a"}}}}}};
                } else if (path == "/api/tasks") {
                    data = {{"active_task_id", "task_a"},
                        {"tasks", QJsonArray{QJsonObject{{"task_id", "task_a"}, {"name", "Task A"}},
                            QJsonObject{{"task_id", "task_b"}, {"name", "Task B"}}}}};
                }
                const auto bytes = QJsonDocument(data).toJson(QJsonDocument::Compact);
                socket->write("HTTP/1.1 " + QByteArray::number(status) +
                    (status == 200 ? " OK\r\n" : " Conflict\r\n") +
                    "Content-Type: application/json\r\nConnection: close\r\nContent-Length: " +
                    QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
                socket->disconnectFromHost();
                buffer->clear();
            });
        }
    });
    const QUrl url(QString("http://127.0.0.1:%1").arg(server.serverPort()));
    Dashboard dashboard(url, 30);
    Workspace workspace(url);
    dashboard.start();
    if (!waitFor([&] { return dashboard.state().value("task").toMap().value("active").toBool(); })) return 3;
    dashboard.command("endTask");
    if (!waitFor([&] { return requests.size() == 1 && dashboard.message() != "Connecting…"; })) return 4;
    // A server-side capture precedes delivery of the HTTP response to Qt.
    // Wait for the client error, including when the old version shows a generic one.
    if (!waitFor([&] { return dashboard.message() == message || dashboard.message().contains("/api/tasks/current/end"); })) {
        qWarning() << "Dashboard response not visible" << dashboard.message();
        return 12;
    }
    const auto direct = requests.first();
    const bool directBody = direct.value("task_id") == "task_a";
    const bool directMessage = dashboard.message() == message;

    QQmlEngine engine;
    engine.rootContext()->setContextProperty("dashboard", &dashboard);
    engine.rootContext()->setContextProperty("workspace", &workspace);
    QQmlComponent component(&engine, QUrl("qrc:/pages/WorkspacePages.qml"));
    if (!waitFor([&] { return component.status() != QQmlComponent::Loading; })) return 5;
    std::unique_ptr<QObject> root(component.createWithInitialProperties({{"page", "sessions"}}));
    if (!root) { qWarning() << component.errors(); return 6; }
    QQuickWindow window;
    window.resize(1200, 800);
    auto *item = qobject_cast<QQuickItem *>(root.get());
    item->setParentItem(window.contentItem());
    item->setSize(QSizeF(1200, 800));
    window.show();
    QObject *button = nullptr, *dialog = nullptr;
    if (!waitFor([&] {
        button = named(root.get(), "session-end");
        dialog = named(root.get(), "end-task-dialog");
        return button && dialog && button->property("enabled").toBool();
    })) {
        qWarning() << "Tasks page not ready" << root->property("cacheStats")
                   << workspace.data() << workspace.errors() << button << dialog;
        return 7;
    }
    if (!QMetaObject::invokeMethod(button, "clicked")) return 8;
    if (!waitFor([&] { return dialog->property("visible").toBool(); })) return 9;
    // Another viewer switches to B while the confirmation for A remains open.
    auto tasks = workspace.data().value("tasks").toMap();
    tasks["active_task_id"] = "task_b";
    workspace.viewData()->insert("tasks", tasks);
    if (!QMetaObject::invokeMethod(dialog, "accept")) return 10;
    if (!waitFor([&] { return requests.size() == 2 && !workspace.busy().value("action").toBool(); })) return 11;
    const auto confirmed = requests.last();
    const bool dialogBody = confirmed.value("task_id") == "task_a";
    const bool workspaceMessage = workspace.errors().value("action").toString() == message;
    dashboard.shutdown();
    QJsonObject result{{"direct_body", direct}, {"dialog_body_after_task_change", confirmed},
        {"direct_task_bound", directBody}, {"dialog_task_bound", dialogBody},
        {"dashboard_message_visible", directMessage}, {"dashboard_message", dashboard.message()},
        {"workspace_message_visible", workspaceMessage}};
    std::puts(QJsonDocument(result).toJson(QJsonDocument::Compact).constData());
    return directBody && dialogBody && directMessage && workspaceMessage ? 0 : 1;
}
