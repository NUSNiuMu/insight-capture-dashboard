#include "dashboard.h"
#include <QGuiApplication>
#include <QJsonDocument>
#include <QTcpServer>
#include <QTcpSocket>
#include <QThread>
#include <memory>

static bool waitFor(const std::function<bool()> &predicate) {
    QElapsedTimer timer; timer.start();
    while (!predicate() && timer.elapsed() < 3000) {
        QCoreApplication::processEvents(QEventLoop::AllEvents, 10);
        QThread::msleep(1);
    }
    return predicate();
}
static void respond(QTcpSocket *socket, const QJsonObject &value) {
    const auto bytes = QJsonDocument(value).toJson(QJsonDocument::Compact);
    socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
        + QByteArray::number(bytes.size()) + "\r\n\r\n" + bytes);
    socket->disconnectFromHost();
}
int main(int argc, char **argv) {
    QGuiApplication app(argc, argv);
    QTcpServer server;
    if (!server.listen(QHostAddress::LocalHost)) return 1;
    QStringList starts, activations;
    QPointer<QTcpSocket> delayedActivation;
    int stops = 0;
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
                for (const auto &line : headers)
                    if (line.toLower().startsWith("content-length:")) length = line.mid(15).trimmed().toInt();
                if (buffer->size() < split + 4 + length) return;
                const auto path = headers.first().split(' ').value(1);
                const auto body = QJsonDocument::fromJson(buffer->mid(split + 4, length)).object();
                buffer->clear();
                if (path == "/api/playback/start") {
                    const auto bag = body["bag_name"].toString(); starts.append(bag);
                    respond(socket, {{"bag_name", bag}, {"state", "ready"},
                        {"manifest_url", "/api/playback/artifacts/" + bag + "/manifest.json"}});
                } else if (path.endsWith("/manifest.json")) {
                    respond(socket, {{"duration_s", 1}, {"fps", 30}, {"frame_count", 30},
                        {"cameras", QJsonArray{QJsonObject{{"name", "test-camera"}, {"video_url", "/video.mp4"}}}},
                        {"poses", QJsonArray{}}});
                } else if (path == "/api/playback/activate") {
                    activations.append(body["bag_name"].toString());
                    delayedActivation = socket;
                } else if (path == "/api/playback/stop") {
                    ++stops; respond(socket, {{"state", "idle"}});
                } else respond(socket, {});
            });
        }
    });
    Dashboard dashboard(QUrl(QString("http://127.0.0.1:%1").arg(server.serverPort())), 30);
    dashboard.command("playback", "bag-a");
    dashboard.command("playback", "bag-b");
    if (!waitFor([&] { return delayedActivation; })) return 2;
    if (starts != QStringList{"bag-a"} || activations != QStringList{"bag-a"}) return 3;
    dashboard.command("live");
    if (!waitFor([&] { return stops == 1; })) return 4;
    dashboard.command("playback", "bag-b"); // Old activation is still owned.
    respond(delayedActivation, {{"state", "playing"}});
    delayedActivation.clear();
    if (!waitFor([&] { return !dashboard.playbackBusy(); })) return 5;
    if (dashboard.playback() || starts.size() != 1) return 6;
    dashboard.command("playback", "bag-b");
    if (!waitFor([&] { return delayedActivation; })) return 7;
    if (activations != QStringList{"bag-a", "bag-b"}) return 8;
    respond(delayedActivation, {{"state", "playing"}});
    delayedActivation.clear();
    if (!waitFor([&] { return dashboard.playback(); })) return 9;
    dashboard.command("live");
    if (!waitFor([&] { return stops == 2 && !dashboard.playbackBusy(); })) return 10;
    return dashboard.playback() ? 11 : 0;
}
