#include "workspace.h"
#include <QJsonDocument>
#include <QNetworkReply>

Workspace::Workspace(QUrl server, QObject *parent) : QObject(parent), m_server(server) {
    m_clock.start();
    for (const char *key : {"tasks","sessions","bags","export","scoring","scoringStart",
            "handpose","capabilities","handFrames","optimization","runs","cameras",
            "trajectories","recording","topics","directories","settings","voice",
            "volume","health","optLog","action"})
        m_viewData.insert(QString::fromLatin1(key), QVariant());
}
void Workspace::invalidate(const QString &key) {
    if (key.isEmpty()) { m_fetchedAt.clear(); ++m_epoch; }
    else { m_fetchedAt.remove(key); ++m_keyEpoch[key]; }
}
void Workspace::ensure(const QString &key, const QString &path, int maxAgeMs) {
    if (m_paths.value(key) == path && m_fetchedAt.contains(key) &&
        m_clock.elapsed() - m_fetchedAt.value(key) < maxAgeMs && !m_errors.contains(key))
        return;
    request(key, path);
}
void Workspace::request(const QString &key, const QString &path, const QString &method,
                        const QVariantMap &body, bool textResponse) {
    const QUrl url = m_server.resolved(QUrl(path));
    if (url.scheme() != m_server.scheme() || url.host() != m_server.host() ||
        url.port() != m_server.port()) {
        m_errors[key] = "结果地址不属于当前后端";
        emit changed();
        emit completed(key, false);
        return;
    }
    if (method == "GET") m_requestedPaths[key] = path;
    if (m_busy.value(key).toBool()) return;
    const qint64 started = m_clock.elapsed();
    const qint64 epoch = m_epoch, keyEpoch = m_keyEpoch.value(key);
    auto stat = m_stats.value(key).toMap();
    stat["requests"] = stat.value("requests").toInt() + 1;
    stat["path"] = path;
    m_stats[key] = stat;
    m_busy[key] = true;
    m_errors.remove(key);
    emit changed();
    QNetworkRequest req(url);
    // Integrity and rosbag enumeration can scan large on-device recordings.
    req.setTransferTimeout(key == "integrity" || key == "bags" || key == "handFrames" ? 120000 : 15000);
    req.setAttribute(QNetworkRequest::CacheLoadControlAttribute, QNetworkRequest::AlwaysNetwork);
    req.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::SameOriginRedirectPolicy);
    req.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
    auto *reply = method == "GET" ? m_http.get(req) :
        m_http.sendCustomRequest(req, method.toUtf8(), QJsonDocument::fromVariant(body).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, this, [this, key, path, method, started, epoch, keyEpoch, reply, textResponse] {
        QJsonParseError parse;
        const auto bytes = reply->readAll();
        const auto document = textResponse ? QJsonDocument() : QJsonDocument::fromJson(bytes, &parse);
        const auto value = textResponse ? QVariant(QString::fromUtf8(bytes)) : document.toVariant();
        const bool ok = reply->error() == QNetworkReply::NoError && parse.error == QJsonParseError::NoError;
        m_busy[key] = false;
        auto stat = m_stats.value(key).toMap();
        stat["last_ms"] = m_clock.elapsed() - started;
        stat["success"] = ok;
        m_stats[key] = stat;
        // A mutation or scope change during a scan must not validate an old response.
        if (method == "GET" && (epoch != m_epoch || keyEpoch != m_keyEpoch.value(key) ||
                               m_requestedPaths.value(key) != path)) {
            reply->deleteLater();
            request(key, m_requestedPaths.value(key), "GET", {}, textResponse);
            return;
        }
        if (ok) {
            if (method != "GET") invalidate();
            m_fetchedAt[key] = m_clock.elapsed();
            m_paths[key] = path;
            // Notify only the updated namespace; cached hidden pages stay idle.
            if (!m_data.contains(key) || m_data.value(key) != value) {
                m_data[key] = value;
                m_viewData.insert(key, value);
            }
        } else
            m_errors[key] = value.toMap().value("error", reply->error() == QNetworkReply::NoError ?
                                               "后端响应不是有效数据" : reply->errorString());
        emit changed();
        emit completed(key, ok);
        reply->deleteLater();
    });
}
