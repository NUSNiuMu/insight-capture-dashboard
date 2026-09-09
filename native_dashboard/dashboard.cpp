#include "dashboard.h"
#include "model.h"
#include <QCryptographicHash>
#include <QDir>
#include <QEventLoop>
#include <QFileInfo>
#include <QJsonDocument>
#include <QNetworkReply>
#include <QPointer>
#include <QSaveFile>
#include <QStandardPaths>
#include <QUrlQuery>
#include <algorithm>
#include <cmath>

static QVector3D vector(const QJsonArray &a) {
    return {float(a[0].toDouble()), float(a[1].toDouble()), float(a[2].toDouble())};
}
static QVector3D basis(const QVector3D &v) { return {-v.y(), v.z(), -v.x()}; }
static QQuaternion basisRotation() {
    QMatrix3x3 m;
    m(0, 0) = 0;
    m(0, 1) = -1;
    m(0, 2) = 0;
    m(1, 0) = 0;
    m(1, 1) = 0;
    m(1, 2) = 1;
    m(2, 0) = -1;
    m(2, 1) = 0;
    m(2, 2) = 0;
    return QQuaternion::fromRotationMatrix(m);
}
Dashboard::Dashboard(QUrl server, int fps, QObject *parent) : QObject(parent), m_server(server), m_fps(fps) {
    m_clock.start();
    m_reconnect.setSingleShot(true);
    connect(&m_poll, &QTimer::timeout, this, &Dashboard::poll);
    connect(&m_reconnect, &QTimer::timeout, this, &Dashboard::connectPose);
    connect(&m_poseSocket, &QWebSocket::textMessageReceived, this, [this](const QString &s) {
        auto p = QJsonDocument::fromJson(s.toUtf8()).object();
        if (p["type"] != "pose_update")
            return;
        m_latestLive = p;
        m_lastPose = m_clock.elapsed();
        m_connected = true;
        if (m_message == "正在连接后端")
            m_message = "后端已连接";
        if (!playback())
            acceptPose(p);
    });
    connect(&m_poseSocket, &QWebSocket::disconnected, this, [this] {
        m_connected = false;
        for (auto *p : m_poseOrder) {
            p->visible = false;
            p->notify();
        }
        emit stateChanged();
        if (!m_stopping)
            m_reconnect.start(1500);
    });
}
void Dashboard::start() {
    poll();
    connectPose();
    m_poll.start(1000);
}
void Dashboard::connectPose() {
    if (m_stopping)
        return;
    QUrl u = m_server;
    u.setScheme(u.scheme() == "https" ? "wss" : "ws");
    u.setPath("/ws");
    m_poseSocket.open(u);
}
QVariantList Dashboard::poses() const {
    QVariantList result;
    for (auto *p : m_poseOrder)
        result.append(QVariant::fromValue(static_cast<QObject *>(p)));
    return result;
}
QVector3D Dashboard::mapPoint(const QJsonArray &p) const { return basis(vector(p) - m_origin) * 100; }
void Dashboard::fetch(const QString &path, std::function<void(QJsonObject)> done) {
    if (m_pending.contains(path))
        return;
    m_pending.insert(path);
    request(path, nullptr, std::move(done));
}
void Dashboard::post(const QString &path, const QJsonObject &body, std::function<void(QJsonObject)> done) {
    if (m_pending.contains(path))
        return;
    m_pending.insert(path);
    request(path, &body, std::move(done));
}
void Dashboard::request(const QString &path, const QJsonObject *body, std::function<void(QJsonObject)> done) {
    QNetworkRequest req(m_server.resolved(QUrl(path)));
    req.setTransferTimeout(path == "/api/rosbags" ? 30000 : 5000);
    QNetworkReply *reply;
    if (body) {
        req.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
        reply = m_http.post(req, QJsonDocument(*body).toJson(QJsonDocument::Compact));
    } else
        reply = m_http.get(req);
    connect(reply, &QNetworkReply::finished, this, [this, reply, path, done] {
        m_pending.remove(path);
        auto value = reply->isOpen() ? QJsonDocument::fromJson(reply->readAll()).object() : QJsonObject();
        if (reply->error() != QNetworkReply::NoError) {
            m_message = value["speech"].toString(value["error"].toString(reply->errorString()));
            if (path.contains("playback")) {
                m_playbackStarting = false;
                if (path != "/api/playback/stop")
                    m_requestedPlayback.clear();
            }
            emit stateChanged();
        } else {
            if (done)
                done(value);
        }
        reply->deleteLater();
    });
}
void Dashboard::poll() {
    fetch("/api/cameras", [this](QJsonObject data) {
        QVariantList list;
        for (auto value : data["cameras"].toArray()) {
            auto c = value.toObject();
            QUrl u = m_server;
            u.setScheme(u.scheme() == "https" ? "wss" : "ws");
            u.setPort(c["webrtc_port"].toInt(8766));
            list.append(QVariantMap{{"name", c["name"].toString()},
                                    {"label", c["label"].toString()},
                                    {"endpoint", u},
                                    {"media", QUrl()}});
        }
        if (list != m_liveCameras) {
            m_liveCameras = list;
            if (!playback()) {
                m_cameras = list;
                emit camerasChanged();
            }
        }
    });
    fetch("/api/recording/status", [this](QJsonObject d) {
        m_state["recording"] = d["recording"].toBool();
        m_state["output"] = d["output_path"].toString();
        m_state["task"] = d["task_status"].toObject().toVariantMap();
        m_state["take"] = d["current_take"].toObject().toVariantMap();
        emit stateChanged();
    });
    fetch("/api/mapping", [this](QJsonObject d) {
        m_state["mapPoints"] = d["map_point_count"].toInt();
        m_state["mapping"] = d["statuses"].toObject().toVariantMap();
        emit stateChanged();
    });
    fetch("/api/playback/status", [this](QJsonObject d) {
        m_state["playbackState"] = d["state"].toString();
        m_state["playbackProgress"] = d["progress"].toDouble();
        if (!m_requestedPlayback.isEmpty() && !m_playbackStarting && !playback() &&
            d.contains("manifest_url"))
            activatePlayback(d);
        emit stateChanged();
    });
    if (m_pollCount++ % 5 == 0) {
        fetch("/api/tasks", [this](QJsonObject d) {
            auto tasks = d["tasks"].toArray().toVariantList();
            if (tasks != m_tasks) {
                m_tasks = tasks;
                emit tasksChanged();
            }
        });
        if (m_pollCount == 1)
            refreshBags();
    }
    if (!playback() && m_clock.elapsed() - m_lastPose > 3000) {
        m_connected = false;
        for (auto *p : m_poseOrder) {
            p->visible = false;
            p->notify();
        }
        emit stateChanged();
    }
}
void Dashboard::refreshBags() {
    fetch("/api/rosbags", [this](QJsonObject d) {
        QVariantList bags;
        for (auto value : d["bags"].toArray()) {
            auto bag = value.toObject();
            bag["selector"] = bag["id"].toString(bag["relative_path"].toString());
            bags.append(bag.toVariantMap());
        }
        if (bags != m_bags) {
            m_bags = bags;
            emit bagsChanged();
        }
    });
}
void Dashboard::clearTraces() {
    for (auto *p : m_poseOrder) {
        p->points.clear();
        p->kept.clear();
        p->generation = -1;
        p->lastSeq = 0;
        p->firstSeq = 1;
        p->geometry->setPoints({});
        p->notify();
    }
}
void Dashboard::setKeepTrail(bool value) {
    m_keep = value;
    for (auto *p : m_poseOrder)
        p->kept.clear();
    emit stateChanged();
}
void Dashboard::acceptPose(const QJsonObject &payload) {
    const auto array = payload["poses"].toArray();
    if (!m_haveOrigin) {
        for (auto v : array) {
            auto p = v.toObject();
            if (p["visible"].toBool() && (p["role"] == "head" || playback())) {
                m_origin = vector(p["position"].toArray());
                m_haveOrigin = true;
                break;
            }
        }
    }
    int capacity = qMax(2, payload["trace_capacity"].toInt(300));
    bool needSnapshot = false;
    for (auto value : array) {
        auto d = value.toObject();
        auto name = d["name"].toString();
        auto *p = m_poseByName.value(name);
        if (!p) {
            p = new Pose(this);
            p->name = name;
            p->role = d["role"].toString();
            p->color = QColor(p->role == "head" ? "#79c47b" : p->role == "left_hand" ? "#79adc2" : "#cf7f6f");
            m_poseByName.insert(name, p);
            m_poseOrder.append(p);
            emit posesChanged();
        }
        p->visible = d["visible"].toBool();
        p->position = mapPoint(d["position"].toArray());
        auto q = d["quaternion_xyzw"].toArray();
        auto b = basisRotation();
        p->rotation = (b * QQuaternion(q[3].toDouble(1), q[0].toDouble(), q[1].toDouble(), q[2].toDouble()) *
                       b.conjugated())
                          .normalized();
        p->modelScale = d["avatar_scale"].toDouble(1) * 20;
        p->modelOffset = basis(vector(d["avatar_offset_xyz"].toArray())) * 100;
        auto r = vector(d["avatar_rotation_deg_xyz"].toArray());
        p->modelRotation = QQuaternion::fromEulerAngles(-r.x(), -r.y(), r.z());
        if (d["gripper_opening"].isDouble())
            p->opening = qBound(0.0, d["gripper_opening"].toDouble(), 1.0);
        cacheModel(p, d);
        auto trace = d["trace_update"].toObject();
        auto points = trace.isEmpty() ? d["trace"].toArray() : trace["points"].toArray();
        if (trace.isEmpty() || trace["mode"] == "snapshot") {
            p->points.clear();
            for (int i = qMax(0, int(points.size()) - capacity); i < points.size(); ++i)
                p->points.append(mapPoint(points[i].toArray()));
            p->generation = trace["generation"].toInteger(0);
            p->lastSeq = trace.isEmpty() ? points.size() : trace["to_seq"].toInteger();
            p->firstSeq = p->lastSeq - p->points.size() + 1;
        } else if (p->generation != trace["generation"].toInteger() ||
                   trace["from_seq"].toInteger() > p->lastSeq + 1) {
            p->points.clear();
            p->geometry->setPoints({});
            needSnapshot = true;
        } else if (trace["to_seq"].toInteger() > p->lastSeq) {
            int overlap = qMax<qint64>(0, p->lastSeq - trace["from_seq"].toInteger() + 1);
            for (int i = overlap; i < points.size(); ++i)
                p->points.append(mapPoint(points[i].toArray()));
            p->lastSeq = trace["to_seq"].toInteger();
            int remove =
                qMin<int>(p->points.size(), qMax<qint64>(qMax(0, int(p->points.size()) - capacity),
                                                         trace["drop_before_seq"].toInteger() - p->firstSeq));
            if (remove > 0) {
                p->points.remove(0, remove);
                p->firstSeq += remove;
            }
        }
        if (m_keep && p->visible && (p->kept.isEmpty() || (p->kept.last() - p->position).length() > .1f))
            p->kept.append(p->position);
        if (m_clock.elapsed() - p->lastGeometry >= 100) {
            p->geometry->setPoints(m_keep ? p->kept : (p->visible ? p->points : QVector<QVector3D>()));
            p->lastGeometry = m_clock.elapsed();
        }
        p->notify();
    }
    if (needSnapshot && !playback()) {
        m_poseSocket.abort();
        m_reconnect.start(250);
    }
}
void Dashboard::cacheModel(Pose *pose, const QJsonObject &data) {
    QString asset = data["asset_url"].toString();
    if (asset.isEmpty() && !data["avatar_model"].toString().isEmpty())
        asset =
            "/asset?path=" + QString::fromLatin1(QUrl::toPercentEncoding(data["avatar_model"].toString()));
    if (asset.isEmpty() || asset == pose->assetKey)
        return;
    pose->assetKey = asset;
    QUrl url = m_server.resolved(QUrl(asset));
    QString dir = QStandardPaths::writableLocation(QStandardPaths::CacheLocation) + "/models-qt62-v2";
    QDir().mkpath(dir);
    QString path =
        dir + "/" + QCryptographicHash::hash(url.toEncoded(), QCryptographicHash::Sha256).toHex() + ".glb";
    if (QFileInfo::exists(path)) {
        pose->modelSource = QUrl::fromLocalFile(path);
        pose->notify();
        return;
    }
    QNetworkRequest request(url);
    request.setTransferTimeout(15000);
    auto *reply = m_http.get(request);
    connect(reply, &QNetworkReply::finished, this, [this, reply, pose, path, asset] {
        if (reply->error() == QNetworkReply::NoError) {
            QSaveFile f(path);
            QString error;
            auto bytes = compatibleGlb(reply->readAll(), error);
            if (bytes.isEmpty()) {
                m_message = "模型转换失败：" + error;
                emit stateChanged();
                reply->deleteLater();
                return;
            }
            if (f.open(QIODevice::WriteOnly) && f.write(bytes) == bytes.size() && f.commit() &&
                pose->assetKey == asset) {
                pose->modelSource = QUrl::fromLocalFile(path);
                pose->notify();
            }
        } else {
            pose->assetKey.clear();
            m_message = "模型下载失败：" + reply->errorString();
            emit stateChanged();
        }
        reply->deleteLater();
    });
}
void Dashboard::command(const QString &action, const QString &value) {
    const bool recording = m_state["recording"].toBool();
    if (recording && (action == "reset" || action == "task" || action == "endTask" || action == "playback")) {
        m_message = "正在录制，暂不能执行该操作";
        emit stateChanged();
        return;
    }
    if (action == "refreshBags") {
        refreshBags();
        return;
    }
    if (playback() && (action == "record" || action == "reset")) {
        m_message = "请先返回实时采集";
        emit stateChanged();
        return;
    }
    if (action == "live") {
        goLive();
        return;
    }
    if (action == "playback") {
        if (value.isEmpty())
            return;
        m_requestedPlayback = value;
        m_playbackStarting = true;
        post("/api/playback/start", {{"bag_name", value}}, [this](QJsonObject d) {
            m_playbackStarting = false;
            m_message = "正在准备回放";
            if (d.contains("manifest_url"))
                activatePlayback(d);
            emit stateChanged();
        });
        return;
    }
    QString path;
    QJsonObject body;
    if (action == "record")
        path = "/api/recording/start";
    else if (action == "stop")
        path = "/api/recording/stop";
    else if (action == "check")
        path = "/api/capture-check/run";
    else if (action == "reset")
        path = "/api/mapping/reset";
    else if (action == "clear")
        path = "/api/trajectory/clear";
    else if (action == "task")
        path = "/api/tasks/" + QString::fromLatin1(QUrl::toPercentEncoding(value)) + "/activate";
    else if (action == "endTask")
        path = "/api/tasks/current/end";
    else if (action == "reject") {
        path = "/api/takes/current/reject";
        body = {{"reason", value.isEmpty() ? "operator_rejected" : value}};
    } else
        return;
    post(path, body, [this, action](QJsonObject d) {
        m_message = d["speech"].toString(d["error"].toString("操作完成"));
        if (action == "stop")
            refreshBags();
        if (action == "clear" || action == "reset") {
            clearTraces();
            if (action == "reset")
                m_haveOrigin = false;
            m_poseSocket.abort();
            m_reconnect.start(250);
        }
        poll();
        emit stateChanged();
    });
}
void Dashboard::activatePlayback(const QJsonObject &status) {
    if (m_playbackStarting || m_requestedPlayback.isEmpty())
        return;
    m_playbackStarting = true;
    fetch(status["manifest_url"].toString(), [this](QJsonObject manifest) {
        if (m_requestedPlayback.isEmpty()) {
            m_playbackStarting = false;
            return;
        }
        post("/api/playback/activate", {{"bag_name", m_requestedPlayback}}, [this, manifest](QJsonObject) {
            m_manifest = manifest;
            m_playbackStarting = false;
            clearTraces();
            m_haveOrigin = false;
            QVariantList list;
            for (auto v : manifest["cameras"].toArray()) {
                auto c = v.toObject();
                list.append(QVariantMap{{"name", c["name"].toString()},
                                        {"label", c["label"].toString()},
                                        {"endpoint", m_server},
                                        {"media", m_server.resolved(QUrl(c["video_url"].toString()))}});
            }
            m_cameras = list;
            emit camerasChanged();
            setPlaybackTime(0);
        });
    });
}
void Dashboard::goLive() {
    m_requestedPlayback.clear();
    if (playback() || m_playbackStarting)
        post("/api/playback/stop", {}, [this](QJsonObject) {
            m_manifest = {};
            m_playbackStarting = false;
            m_haveOrigin = false;
            clearTraces();
            m_cameras = m_liveCameras;
            emit camerasChanged();
            m_poseSocket.abort();
            m_reconnect.start(150);
        });
}
void Dashboard::setPlaybackTime(double seconds) {
    if (!playback())
        return;
    double frame =
        qBound(0.0, seconds * m_manifest["fps"].toDouble(30), double(m_manifest["frame_count"].toInt() - 1));
    int low = int(frame), high = qMin(low + 1, m_manifest["frame_count"].toInt() - 1);
    float alpha = frame - low;
    QJsonArray poses;
    for (auto v : m_manifest["poses"].toArray()) {
        auto p = v.toObject();
        auto positions = p["positions"].toArray(), quaternions = p["quaternions_xyzw"].toArray(),
             valid = p["valid"].toArray();
        if (high >= positions.size() || high >= quaternions.size())
            continue;
        auto a = vector(positions[low].toArray()), b = vector(positions[high].toArray());
        auto x = a * (1 - alpha) + b * alpha;
        auto qa = quaternions[low].toArray(), qb = quaternions[high].toArray();
        auto q = QQuaternion::slerp(
            QQuaternion(qa[3].toDouble(1), qa[0].toDouble(), qa[1].toDouble(), qa[2].toDouble()),
            QQuaternion(qb[3].toDouble(1), qb[0].toDouble(), qb[1].toDouble(), qb[2].toDouble()), alpha);
        p["position"] = QJsonArray{x.x(), x.y(), x.z()};
        p["quaternion_xyzw"] = QJsonArray{q.x(), q.y(), q.z(), q.scalar()};
        p["visible"] = valid[qBound(0, qRound(frame), int(valid.size()) - 1)].toBool();
        QJsonArray trace;
        for (int i = 0; i <= low && i < positions.size(); ++i)
            if (valid[i].toBool())
                trace.append(positions[i]);
        p["trace"] = trace;
        p.remove("trace_update");
        poses.append(p);
    }
    acceptPose({{"poses", poses}, {"trace_capacity", qMax(300, int(frame) + 1)}});
}
void Dashboard::updateGripper(QObject *root, double opening) {
    if (!root)
        return;
    for (auto *node : root->findChildren<QObject *>()) {
        const auto name = node->objectName();
        if (!name.contains("finger_holder"))
            continue;
        QVariant value = node->property("position");
        if (!value.canConvert<QVector3D>())
            continue;
        auto rest = node->property("nativeRest");
        if (!rest.isValid()) {
            node->setProperty("nativeRest", value);
            rest = value;
        }
        auto pos = rest.value<QVector3D>();
        float travel = qMax(0.f, std::abs(pos.x()) - .001f);
        pos.setX(pos.x() + (pos.x() < 0 ? 1 : -1) * travel * (1 - qBound(0.0, opening, 1.0)));
        node->setProperty("position", QVariant::fromValue(pos));
    }
}
QVariantMap Dashboard::diagnostics() const {
    QVariantList data;
    for (auto *p : m_poseOrder)
        data.append(QVariantMap{{"name", p->name},
                                {"visible", p->visible},
                                {"points", p->points.size()},
                                {"model", p->modelSource.toString()}});
    return {{"connected", m_connected}, {"poses", data}, {"playback", playback()}, {"message", m_message}};
}
void Dashboard::shutdown() {
    m_stopping = true;
    m_poll.stop();
    m_reconnect.stop();
    m_poseSocket.abort();
    if (playback()) {
        QEventLoop loop;
        QTimer timer;
        timer.setSingleShot(true);
        connect(&timer, &QTimer::timeout, &loop, &QEventLoop::quit);
        QPointer<QEventLoop> waiting = &loop;
        post("/api/playback/stop", {}, [waiting](QJsonObject) {
            if (waiting)
                waiting->quit();
        });
        timer.start(1500);
        loop.exec();
    }
}
