#include "workspace.h"
#include <QJsonDocument>
#include <QNetworkReply>

void Workspace::request(const QString &key, const QString &path, const QString &method,
                        const QVariantMap &body, bool textResponse) {
    if (m_busy.value(key).toBool())
        return;
    const QUrl url = m_server.resolved(QUrl(path));
    if (url.scheme() != m_server.scheme() || url.host() != m_server.host() ||
        url.port() != m_server.port()) {
        m_errors[key] = "结果地址不属于当前后端";
        emit changed();
        emit completed(key, false);
        return;
    }
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
    connect(reply, &QNetworkReply::finished, this, [this, key, reply, textResponse] {
        QJsonParseError parse;
        const auto bytes = reply->readAll();
        const auto document = textResponse ? QJsonDocument() : QJsonDocument::fromJson(bytes, &parse);
        const auto value = textResponse ? QVariant(QString::fromUtf8(bytes)) : document.toVariant();
        const bool ok = reply->error() == QNetworkReply::NoError && parse.error == QJsonParseError::NoError;
        m_busy[key] = false;
        if (ok)
            m_data[key] = value;
        else
            m_errors[key] = value.toMap().value("error", reply->error() == QNetworkReply::NoError ?
                                               "后端响应不是有效数据" : reply->errorString());
        emit changed();
        emit completed(key, ok);
        reply->deleteLater();
    });
}
