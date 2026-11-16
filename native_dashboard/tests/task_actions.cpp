#include "dashboard.h"
#include "workspace.h"
#include <QGuiApplication>
#include <QQmlComponent>
#include <QQmlContext>
#include <QQmlEngine>
#include <QQmlExpression>
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
    const QString reviewMessage = QStringLiteral("录次已变化，作废请求未执行，请刷新状态。");
    QList<QJsonObject> requests;
    QStringList requestPaths;
    bool fixtureRecording = false;
    const QString fixtureOutput = "/bags/recording_a";
    QString fixtureMode = "vio_calibration";
    bool fixtureOperationBusy = false;
    QString fixtureTaskId = "task_a";
    const qint64 fixtureTakeId = 1;
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
                    requestPaths.append(QString::fromUtf8(path));
                    data = {{"message", path == "/api/takes/current/reject" ? reviewMessage : message}};
                    status = 409;
                } else if (path == "/api/recording/status") {
                    data = {{"recording", fixtureRecording}, {"merge_state", "idle"},
                        {"output_path", fixtureOutput}, {"recording_mode", fixtureMode},
                        {"operation_in_progress", fixtureOperationBusy},
                        {"task_status", QJsonObject{{"active", true},
                            {"task", QJsonObject{{"task_id", fixtureTaskId}}}}},
                        {"current_take", QJsonObject{{"task_id", fixtureTaskId}, {"take_id", fixtureTakeId}, {"task_name", fixtureTaskId}}}};
                } else if (path == "/api/recording/topics") {
                    data = {{"topics", QJsonArray{"/camera"}}, {"default_selected_topics", QJsonArray{"/camera"}}};
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
    QQmlExpression startExpression(engine.rootContext(), nullptr, QStringLiteral("dashboard.command('record')"));
    startExpression.evaluate();
    if (startExpression.hasError()) { qWarning() << startExpression.error(); return 13; }
    if (!waitFor([&] { return requests.size() == 3 && dashboard.state().value("captureAction").toString().isEmpty(); })) return 13;
    const auto directStart = requests.last();
    const bool directStartBound = directStart.value("task_id") == "task_a" && directStart.value("native_relaxed_preflight").toBool();
    root->setProperty("page", "recording");
    QObject *startButton = nullptr;
    if (!waitFor([&] {
        startButton = named(root.get(), "recording-start");
        return startButton && startButton->property("enabled").toBool();
    })) {
        qWarning() << "Recorder page not ready" << workspace.data() << workspace.errors();
        return 14;
    }
    if (!QMetaObject::invokeMethod(startButton, "clicked")) return 15;
    if (!waitFor([&] { return requests.size() == 4 && !workspace.busy().value("action").toBool(); })) return 16;
    const auto recorderStart = requests.last();
    const bool recorderStartBound = recorderStart.value("task_id") == "task_a" &&
        recorderStart.value("native_relaxed_preflight").toBool() &&
        recorderStart.value("topics").toArray() == QJsonArray{"/camera"};
    fixtureRecording = true;
    if (!waitFor([&] { return dashboard.state().value("recording").toBool(); })) return 17;
    dashboard.command("stop");
    if (!waitFor([&] { return requests.size() == 5 && dashboard.state().value("captureAction").toString().isEmpty(); })) return 18;
    const auto directStop = requests.last();
    const bool directStopBound = directStop.value("output_path") == fixtureOutput && directStop.value("recording_mode") == fixtureMode;
    QObject *stopButton = nullptr;
    if (!waitFor([&] {
        stopButton = named(root.get(), "recording-stop");
        return stopButton && stopButton->property("enabled").toBool();
    })) return 19;
    if (!QMetaObject::invokeMethod(stopButton, "clicked")) return 20;
    if (!waitFor([&] { return requests.size() == 6 && !workspace.busy().value("action").toBool(); })) return 21;
    const auto recorderStop = requests.last();
    const bool recorderStopBound = recorderStop.value("output_path") == fixtureOutput && recorderStop.value("recording_mode") == fixtureMode;
    fixtureRecording = false;
    if (!waitFor([&] { return !dashboard.state().value("recording").toBool(); })) return 22;
    QQmlComponent confirmationComponent(&engine, QUrl("qrc:/ui/ActionConfirmation.qml"));
    if (!waitFor([&] { return confirmationComponent.status() != QQmlComponent::Loading; })) return 23;
    std::unique_ptr<QObject> confirmation(confirmationComponent.createWithInitialProperties({
        {"controller", QVariant::fromValue(&dashboard)}, {"parent", QVariant::fromValue(window.contentItem())}}));
    if (!confirmation) { qWarning() << confirmationComponent.errors(); return 24; }
    auto openReview = [&] {
        return QMetaObject::invokeMethod(confirmation.get(), "showFor",
            Q_ARG(QVariant, QVariant("reject")), Q_ARG(QVariant, QVariant("operator_rejected")),
            Q_ARG(QVariant, QVariant("Reject the last take?")), Q_ARG(QVariant, dashboard.state().value("take")));
    };
    if (!openReview() || !waitFor([&] { return confirmation->property("visible").toBool(); })) return 25;
    fixtureTaskId = "task_b"; // Same numeric Take, different task, while dialog stays open.
    if (!waitFor([&] { return dashboard.state().value("take").toMap().value("task_id") == "task_b"; })) return 26;
    if (!QMetaObject::invokeMethod(confirmation.get(), "accept")) return 27;
    if (!waitFor([&] { return requests.size() == 7 && dashboard.message() == reviewMessage; })) return 28;
    const auto rejectBody = requests.last();
    const bool rejectBound = rejectBody.value("task_id") == "task_a" && rejectBody.value("take_id").toInteger() == fixtureTakeId;
    if (!waitFor([&] { return !confirmation->property("visible").toBool(); }) || !openReview()) return 29;
    if (!waitFor([&] { return confirmation->property("visible").toBool(); })) return 30;
    if (!QMetaObject::invokeMethod(confirmation.get(), "reject")) return 31;
    if (!waitFor([&] { return !confirmation->property("visible").toBool(); }) || requests.size() != 7) return 32;
    if (!openReview() || !waitFor([&] { return confirmation->property("visible").toBool(); })) return 33;
    if (!QMetaObject::invokeMethod(confirmation.get(), "accept") || !waitFor([&] { return requests.size() == 8; })) return 34;
    const auto reopenedReview = requests.last();
    const bool reviewReopened = reopenedReview.value("task_id") == "task_b" && reopenedReview.value("take_id").toInteger() == fixtureTakeId;
    if (!waitFor([&] { return !confirmation->property("visible").toBool(); })) return 35;
    if (!QMetaObject::invokeMethod(confirmation.get(), "showFor",
        Q_ARG(QVariant, QVariant("reset")), Q_ARG(QVariant, QVariant("")),
        Q_ARG(QVariant, QVariant("Reset the current map?")), Q_ARG(QVariant, QVariant(QVariantMap())))) return 36;
    if (!waitFor([&] { return confirmation->property("visible").toBool(); })) return 37;
    if (!QMetaObject::invokeMethod(confirmation.get(), "accept") || !waitFor([&] { return requests.size() == 9; })) return 38;
    const bool resetConfirmationKept = requestPaths.last() == "/api/mapping/reset" && requests.last().isEmpty();
    fixtureMode = "capture";
    fixtureRecording = true;
    if (!waitFor([&] { return dashboard.state().value("recording").toBool() && dashboard.state().value("recordingMode") == "capture"; })) return 39;
    dashboard.command("reset");
    if (!waitFor([&] { return requests.size() == 10 || dashboard.message() == "Stop recording first."; })) return 40;
    const bool captureResetAllowed = requests.size() == 10 && requestPaths.last() == "/api/mapping/reset" && dashboard.property("canResetMapping").toBool();
    fixtureMode = "vio_calibration";
    if (!waitFor([&] { return dashboard.state().value("recordingMode") == "vio_calibration"; })) return 41;
    const int beforeRawReset = requests.size();
    dashboard.command("reset");
    const bool rawCalibrationResetBlocked = !dashboard.property("canResetMapping").toBool() &&
        requests.size() == beforeRawReset && dashboard.message() == "Stop recording first.";
    fixtureMode = "capture";
    fixtureOperationBusy = true;
    if (!waitFor([&] { return dashboard.state().value("recordingMode") == "capture" && dashboard.state().value("recordingBusy").toBool(); })) return 42;
    const int beforeBusyReset = requests.size();
    dashboard.command("reset");
    const bool transitionResetBlocked = !dashboard.property("canResetMapping").toBool() &&
        requests.size() == beforeBusyReset && dashboard.message() == "Wait for the capture operation to finish.";
    dashboard.shutdown();
    QJsonObject result{{"direct_body", direct}, {"dialog_body_after_task_change", confirmed},
        {"direct_task_bound", directBody}, {"dialog_task_bound", dialogBody},
        {"dashboard_message_visible", directMessage}, {"dashboard_message", dashboard.message()},
        {"workspace_message_visible", workspaceMessage}, {"direct_start_body", directStart},
        {"recorder_start_body", recorderStart}, {"direct_start_task_bound", directStartBound},
        {"recorder_start_task_bound", recorderStartBound}, {"direct_stop_body", directStop},
        {"recorder_stop_body", recorderStop}, {"direct_stop_bound", directStopBound},
        {"recorder_stop_bound", recorderStopBound}, {"reject_body", rejectBody}, {"reject_bound", rejectBound},
        {"review_reopened_body", reopenedReview}, {"review_reopened", reviewReopened},
        {"reset_confirmation_kept", resetConfirmationKept}, {"capture_reset_allowed", captureResetAllowed},
        {"raw_calibration_reset_blocked", rawCalibrationResetBlocked}, {"transition_reset_blocked", transitionResetBlocked}};
    std::puts(QJsonDocument(result).toJson(QJsonDocument::Compact).constData());
    return directBody && dialogBody && directMessage && workspaceMessage && directStartBound && recorderStartBound && directStopBound && recorderStopBound && rejectBound && reviewReopened && resetConfirmationKept && captureResetAllowed && rawCalibrationResetBlocked && transitionResetBlocked ? 0 : 1;
}
