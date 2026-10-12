#pragma once
#include <QNetworkAccessManager>
#include <QObject>
#include <QUrl>
#include <QVariantMap>

class Workspace : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantMap data READ data NOTIFY changed)
    Q_PROPERTY(QVariantMap errors READ errors NOTIFY changed)
    Q_PROPERTY(QVariantMap busy READ busy NOTIFY changed)
  public:
    explicit Workspace(QUrl server, QObject *parent = nullptr) : QObject(parent), m_server(server) {}
    QVariantMap data() const { return m_data; }
    QVariantMap errors() const { return m_errors; }
    QVariantMap busy() const { return m_busy; }
    Q_INVOKABLE QUrl resolve(const QString &path) const { return m_server.resolved(QUrl(path)); }
    Q_INVOKABLE void request(const QString &key, const QString &path,
                             const QString &method = "GET", const QVariantMap &body = {}, bool textResponse = false);
  signals:
    void changed();
    void completed(const QString &key, bool success);
  private:
    QUrl m_server;
    QNetworkAccessManager m_http;
    QVariantMap m_data, m_errors, m_busy;
};
