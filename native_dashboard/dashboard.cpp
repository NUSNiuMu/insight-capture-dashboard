#include "dashboard.h"
#include <chrono>
#include "model.h"
#include <QClipboard>
#include <QGuiApplication>
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
        const bool connectionChanged = !m_connected;
        m_connected = true;
        if (m_message == "Connecting…")
            m_message = "Connected";
        if (!playback())
            acceptPose(p);
        if (connectionChanged)
            emit stateChanged();
    });
    connect(&m_poseSocket, &QWebSocket::disconnected, this, [this] {
        m_connected = false;
        if (!playback()) for (auto *p : m_poseOrder) {
            p->visible = false;
            p->notify();
        }
        emit stateChanged();
        if (!m_stopping)
            m_reconnect.start(1500);
    });
}
void Dashboard::setSpatialActive(bool value) {
    if (m_spatialActive == value) return;
    m_spatialActive = value;
    if (value) for (auto *p : m_poseOrder) {
        if (p->geometryDirty && p->trailEnabled) {
            p->geometry->setPoints(m_keep ? p->kept : p->points);
            p->geometryDirty = false;
            p->lastGeometry = m_clock.elapsed();
        }
        p->notify();
    }
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
    req.setTransferTimeout(path.startsWith("/api/recording/") && body ? 60000 : path == "/api/rosbags" ? 30000 : 5000);
    QNetworkReply *reply;
    if (body) {
        req.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
        reply = m_http.post(req, QJsonDocument(*body).toJson(QJsonDocument::Compact));
    } else
        reply = m_http.get(req);
    connect(reply, &QNetworkReply::finished, this, [this, reply, path, done] {
        m_pending.remove(path);
        const bool captureCommand = path == "/api/recording/start" || path == "/api/recording/stop";
        if (captureCommand)
            m_state["captureAction"] = "";
        auto value = reply->isOpen() ? QJsonDocument::fromJson(reply->readAll()).object() : QJsonObject();
        if (reply->error() != QNetworkReply::NoError) {
            m_message = value["error"].toString(reply->errorString());
            m_transportError = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).isNull()
                ? m_message : QString();
            if (captureCommand)
                m_state["captureError"] = m_message;
            if (path.contains("playback")) {
                m_playbackStarting = false;
                if (path != "/api/playback/stop")
                    m_requestedPlayback.clear();
            }
            emit stateChanged();
        } else {
            if (!m_transportError.isEmpty() && m_message == m_transportError) {
                m_message.clear();
                emit stateChanged();
            }
            m_transportError.clear();
            if (done)
                done(value);
            if (captureCommand)
                emit stateChanged();
        }
        reply->deleteLater();
    });
}
void Dashboard::poll() {
    updateLocalization();
    fetch("/api/cameras", [this](QJsonObject data) {
        QVariantList list;
        QVariantMap availability;
        QVariantMap vioFresh;
        for (auto value : data["cameras"].toArray()) {
            auto c = value.toObject();
            availability[c["name"].toString()] = !c["stale"].toBool();
            const auto device = c["device_name"].toString(c["name"].toString());
            vioFresh[device] = vioFresh.value(device).toBool() || c["native_vio_fresh"].toBool(true);
            QUrl u = m_server;
            u.setScheme(u.scheme() == "https" ? "wss" : "ws");
            u.setPort(c["webrtc_port"].toInt(8766));
            QUrl local;
            if (c["native_preview_available"].toBool() &&
                (m_server.host() == "127.0.0.1" || m_server.host() == "localhost" || m_server.host() == "::1") &&
                qEnvironmentVariable("INSIGHT_NATIVE_TRANSPORT") != "webrtc") {
                local = m_server.resolved(QUrl("/ws/native"));
                local.setScheme(local.scheme() == "https" ? "wss" : "ws");
            }
            list.append(QVariantMap{{"name", c["name"].toString()},
                                    {"device", c["device_name"].toString(c["name"].toString())},
                                    // Preview cache updates must not replace live video items.
                                    {"width", c["source_width"].toInt()},
                                    {"height", c["source_height"].toInt()},
                                    {"row", c["row"].toInt()},
                                    {"column", c["column"].toInt()},
                                    {"label", c["label"].toString()},
                                    {"endpoint", u},
                                    {"nativeEndpoint", local},
                                    {"media", QUrl()}});
        }
        if (availability != m_cameraAvailability) {
            m_cameraAvailability = availability;
            emit availabilityChanged();
        }
        if (list != m_liveCameras) {
            m_liveCameras = list;
            if (!playback()) {
                m_cameras = list;
                emit camerasChanged();
            }
        }
        m_lastCameraUpdate = m_clock.elapsed();
        m_nativeVioFresh = vioFresh;
        updateLocalization();
    });
    fetch("/api/recording/status", [this](QJsonObject d) {
        const auto previous = m_state;
        m_state["recording"] = d["recording"].toBool();
        m_state["output"] = d["output_path"].toString();
        m_state["distributed"] = d["distributed"].toObject().toVariantMap();
        m_state["recordingBusy"] = d["merge_state"].toString() == "merging" || d["merge_state"].toString() == "finalizing" || m_pending.contains("/api/recording/start") || m_pending.contains("/api/recording/stop");
        m_state["task"] = d["task_status"].toObject().toVariantMap();
        m_state["take"] = d["current_take"].toObject().toVariantMap();
        if (m_state != previous)
            emit stateChanged();
    });
    fetch("/api/mapping", [this](QJsonObject d) {
        const auto previousPoints = m_state.value("mapPoints");
        m_state["mapPoints"] = d["map_point_count"].toInt();
        m_state["mapping"] = d["statuses"].toObject().toVariantMap();
        m_lastMappingUpdate = m_clock.elapsed();
        updateLocalization();
        if (previousPoints != m_state.value("mapPoints"))
            emit stateChanged();
    });
    fetch("/api/playback/status", [this](QJsonObject d) {
        const auto previous = m_state;
        m_state["playbackState"] = d["state"].toString();
        m_state["playbackProgress"] = d["progress"].toDouble();
        if (!m_requestedPlayback.isEmpty() && d["state"] == "error") {
            m_message = d["error"].toString();
            m_requestedPlayback.clear();
        }
        if (!m_requestedPlayback.isEmpty() && !m_playbackStarting && !playback() &&
            d.contains("manifest_url"))
            activatePlayback(d);
        if (m_state != previous)
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
        const bool changed = m_connected;
        m_connected = false;
        for (auto *p : m_poseOrder) {
            if (p->visible) {
                p->visible = false;
                emit p->poseChanged();
            }
        }
        if (changed)
            emit stateChanged();
    }
}

QVariantMap Dashboard::localizationSummary(const QVariantMap &status, bool vioFresh, bool head) {
    auto result = [](const QString &mode, const QString &label, const QString &color) {
        return QVariantMap{{"mode", mode}, {"label", label}, {"color", color}};
    };
    const QString waiting = "#84775f", warning = "#aa741b", good = "#417b51", bad = "#b45d51";
    if (!vioFresh)
        return result("waiting_pose", "Pose input lost", warning);
    if (!status.value("online").toBool())
        return result("service_offline", head ? "Mapper offline" : "Localizer offline", bad);
    if (status.value("state") == "error")
        return result("error", head ? "Mapping error" : "Localization error", bad);
    if (status.value("state") == "waiting_for_inference")
        return result("waiting_inference", "Inference unavailable", warning);
    if (status.value("state") == "waiting_for_frame_pose")
        return result("waiting_frame_pose", "Waiting for frame pose", waiting);
    if (status.value("state") == "waiting_for_inputs")
        return result("waiting_inputs", "Waiting for mapping input", waiting);
    const auto tracking = status.value("tracking_status").toString();
    if (tracking != "TRACKING" && tracking != "TRACKING_STATIC")
        return result("waiting_tracking", "SDK tracking lost", warning);
    if (!status.value("clock_ready").toBool())
        return result("waiting_clock", "Synchronizing clock", waiting);
    if (head) {
        if (status.value("state") == "building")
            return result("building", "Building map", warning);
        return result("mapping", "Mapping", good);
    }
    const auto mode = status.value("tracking_mode").toString();
    if (mode == "map_match" && status.value("confirmed").toBool())
        return result("map_matched", "Map matched", good);
    if (status.value("localized").toBool())
        return result("vio_only", "VIO prediction", warning);
    if (status.value("reason") == "map_not_ready")
        return result("waiting_map", "Waiting for map", waiting);
    return result("unlocalized", "Not localized", warning);
}

void Dashboard::updateLocalization() {
    QVariantMap counts, summaries;
    for (const auto &value : m_liveCameras) {
        const auto camera = value.toMap();
        const auto device = camera.value("device", camera.value("name")).toString();
        counts[device] = counts.value(device).toInt() + 1;
    }
    const auto statuses = m_state.value("mapping").toMap();
    const auto now = m_clock.elapsed();
    for (auto it = counts.begin(); it != counts.end(); ++it) {
        const auto device = it.key();
        const bool head = !device.startsWith("insight3_");
        auto status = statuses.value(head ? "insight9" : device).toMap();
        if (m_lastMappingUpdate < 0 || now - m_lastMappingUpdate > 2500)
            status["online"] = false;
        auto summary = localizationSummary(status, m_nativeVioFresh.value(device, false).toBool(), head);
        auto missing = status.value("missing_inputs").toStringList();
        summary["detail"] = status.value("error").toString();
        if (summary["detail"].toString().isEmpty())
            summary["detail"] = missing.isEmpty() ? status.value("reason").toString() : missing.join("\n");
        summaries[device] = summary;
    }
    if (summaries != m_localization) {
        m_localization = summaries;
        emit localizationChanged();
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
        p->geometryDirty = false;
        p->displayFilter.reset();
        p->notify();
    }
}
void Dashboard::setKeepTrail(bool value) {
    m_keep = value;
    for (auto *p : m_poseOrder) {
        p->kept.clear();
        p->keepMode = value;
        p->geometryDirty = true;
        emit p->traceChanged();
    }
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
            p->keepMode = m_keep;
            p->color = QColor(p->role == "head" ? "#79c47b" : p->role == "left_hand" ? "#79adc2" : "#cf7f6f");
            m_poseByName.insert(name, p);
            m_poseOrder.append(p);
            emit posesChanged();
        }
        const auto previousPosition = p->position;
        const auto previousRotation = p->rotation;
        const auto previousDisplayPosition = p->displayPosition();
        const auto previousDisplayRotation = p->displayRotation();
        const auto previousOffset = p->modelOffset;
        const auto previousModelRotation = p->modelRotation;
        const auto previousScale = p->modelScale;
        const auto previousVisible = p->visible;
        const auto previousSeq = p->lastSeq;
        const auto previousGeneration = p->generation;
        const auto previousFirstSeq = p->firstSeq;
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
        // Babylon's left-handed glTF loader reflects X; our scene reflects Z.
        // Their difference is a local Y half-turn, after the configured rotation.
        p->modelRotation = QQuaternion::fromEulerAngles(-r.x(), -r.y(), r.z()) *
                           QQuaternion::fromAxisAndAngle(0, 1, 0, 180);
        cacheModel(p, d);
        auto trace = d["trace_update"].toObject();
        auto points = trace.isEmpty() ? d["trace"].toArray() : trace["points"].toArray();
        const bool snapshot = trace.isEmpty() || trace["mode"] == "snapshot";
        const auto previousPoints = snapshot ? p->points : QVector<QVector3D>();
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
        bool keptChanged = false;
        if (m_keep && p->visible && (p->kept.isEmpty() || (p->kept.last() - p->position).length() > .1f)) {
            p->kept.append(p->position);
            // Bound display history while retaining the full path's extent.
            if (p->kept.size() > 4096) {
                QVector<QVector3D> reduced;
                reduced.reserve(p->kept.size() / 2 + 1);
                for (int i = 0; i < p->kept.size() - 1; i += 2)
                    reduced.append(p->kept[i]);
                reduced.append(p->kept.last());
                p->kept = std::move(reduced);
            }
            keptChanged = true;
        }
        const bool traceChanged = previousSeq != p->lastSeq || previousGeneration != p->generation ||
                                  previousFirstSeq != p->firstSeq || keptChanged ||
                                  (snapshot && previousPoints != p->points);
        p->displayFilter.update(p->position, p->rotation, m_clock.elapsed(),
            playback() || !previousVisible || !p->visible || previousGeneration != p->generation);
        p->geometryDirty |= (m_keep ? keptChanged : traceChanged) || previousVisible != p->visible;
        // Consume every trace delta while hidden, but defer scene notifications and geometry.
        if (!m_spatialActive) continue;
        if (p->trailEnabled && p->geometryDirty && m_clock.elapsed() - p->lastGeometry >= 100) {
            p->geometry->setPoints(m_keep ? p->kept : p->points);
            p->lastGeometry = m_clock.elapsed();
            p->geometryDirty = false;
        }
        if (previousVisible != p->visible || previousPosition != p->position || previousRotation != p->rotation ||
            previousDisplayPosition != p->displayPosition() || previousDisplayRotation != p->displayRotation())
            emit p->poseChanged();
        if (previousOffset != p->modelOffset || previousModelRotation != p->modelRotation || previousScale != p->modelScale)
            emit p->modelChanged();
        if (traceChanged)
            emit p->traceChanged();
        if (qEnvironmentVariable("INSIGHT_LATENCY_TRACE") == "1" && d.contains("latency_trace")) {
            auto timing = d["latency_trace"].toObject().toVariantMap();
            timing["name"] = p->name;
            timing["display_position_lag_mm"] = (p->position - p->displayPosition()).length() * 10;
            timing["applied_ns"] = qlonglong(std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now().time_since_epoch()).count());
            m_poseTrace.append(timing);
            while (m_poseTrace.size() > 360)
                m_poseTrace.removeFirst();
        }
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
                m_message = "Model conversion failed: " + error;
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
            m_message = "Model download failed: " + reply->errorString();
            emit stateChanged();
        }
        reply->deleteLater();
    });
}
void Dashboard::copyDiagnostics(const QVariantList &videos) {
    fetch("/api/diagnostics", [this, videos](QJsonObject data) {
        data["qt"] = QJsonObject::fromVariantMap(diagnostics());
        data["videos"] = QJsonArray::fromVariantList(videos);
        QGuiApplication::clipboard()->setText(QString::fromUtf8(
            QJsonDocument(data).toJson(QJsonDocument::Indented)));
        m_message = "Diagnostics copied";
        emit stateChanged();
    });
}
void Dashboard::command(const QString &action, const QString &value) {
    const bool recording = m_state["recording"].toBool();
    if (recording && (action == "reset" || action == "task" || action == "endTask" || action == "playback")) {
        m_message = "Stop recording first.";
        emit stateChanged();
        return;
    }
    if (action == "refreshBags") {
        refreshBags();
        return;
    }
    if (playback() && (action == "record" || action == "reset")) {
        m_message = "Return to Live first.";
        emit stateChanged();
        return;
    }
    if (action == "record" && !m_state["task"].toMap().value("active").toBool()) {
        m_message = "Select a task first.";
        m_state["captureError"] = m_message;
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
            m_message = "Preparing playback…";
            if (d.contains("manifest_url"))
                activatePlayback(d);
            emit stateChanged();
        });
        return;
    }
    QString path;
    QJsonObject body;
    if (action == "record") {
        path = "/api/recording/start";
        body = {{"native_relaxed_preflight", true}};
    } else if (action == "stop")
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
    if (action == "record" || action == "stop") {
        if (m_pending.contains(path))
            return;
        m_state["captureAction"] = action == "record" ? "starting" : "stopping";
        m_state["captureError"] = "";
        m_message = action == "record" ? "Checking devices and clocks…" : "Stopping recording…";
        emit stateChanged();
    }
    post(path, body, [this, action](QJsonObject d) {
        m_message = d["error"].toString(action == "record" ? "Recording started" : "Done");
        if (action == "task" || action == "endTask") {
            m_state["task"] = d.toVariantMap();
            m_state["captureError"] = "";
        }
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
            m_message = manifest["quality"].toObject()["state"] == "warning"
                            ? "Playback ready · Quality warnings" : "Playback ready";
            QStringList missing;
            for (auto name : manifest["quality"].toObject()["missing_pose_tracks"].toArray())
                missing.append(name.toString());
            if (!missing.isEmpty())
                m_message = "Playback ready · No global pose: " + missing.join(", ");
            m_playbackStarting = false;
            clearTraces();
            for (auto *pose : m_poseOrder) {
                pose->visible = false;
                pose->notify();
            }
            m_haveOrigin = false;
            QVariantList list;
            for (auto v : manifest["cameras"].toArray()) {
                auto c = v.toObject();
                list.append(QVariantMap{{"name", c["name"].toString()},
                                        {"label", c["label"].toString()},
                                        {"row", c["row"].toInt()},
                                        {"column", c["column"].toInt()},
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
    const bool requested = !m_requestedPlayback.isEmpty();
    m_requestedPlayback.clear();
    if (playback() || m_playbackStarting || requested || m_state["playbackState"] == "preparing")
        post("/api/playback/stop", {}, [this](QJsonObject) {
            m_manifest = {};
            m_playbackStarting = false;
            m_message = "Live";
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
        int lastValid = -1;
        for (int i = 0; i <= low && i < positions.size(); ++i)
            if (valid[i].toBool()) {
                trace.append(positions[i]);
                lastValid = i;
            }
        // A pose gap does not erase the last recorded device from the review scene.
        if (!p["visible"].toBool() && lastValid >= 0) {
            p["position"] = positions[lastValid];
            p["quaternion_xyzw"] = quaternions[lastValid];
        }
        p["trace"] = trace;
        p.remove("trace_update");
        poses.append(p);
    }
    acceptPose({{"poses", poses}, {"trace_capacity", qMax(300, int(frame) + 1)}});
}
QVariantMap Dashboard::diagnostics() const {
    QVariantList data;
    for (auto *p : m_poseOrder)
        data.append(QVariantMap{{"name", p->name},
                                {"visible", p->visible},
                                {"points", p->points.size()},
                                {"kept_points", p->kept.size()},
                                {"trace_seq", p->lastSeq},
                                {"trace_generation", p->generation},
                                {"geometry_updates", qulonglong(p->geometry->revision())},
                                {"model", p->modelSource.toString()}});
    return {{"connected", m_connected}, {"spatial_active", m_spatialActive}, {"poses", data}, {"pose_trace", m_poseTrace},
            {"localization", m_localization},
            {"playback", playback()}, {"message", m_message}};
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
