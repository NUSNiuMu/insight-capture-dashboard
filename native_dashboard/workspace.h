#pragma once
#include <QNetworkAccessManager>
#include <QObject>
#include <QElapsedTimer>
#include <QQmlPropertyMap>
#include <QUrl>
#include <QVariantMap>

class Workspace : public QObject {
    Q_OBJECT
    Q_PROPERTY(QQmlPropertyMap *data READ viewData CONSTANT)
    Q_PROPERTY(QVariantMap errors READ errors NOTIFY changed)
    Q_PROPERTY(QVariantMap busy READ busy NOTIFY changed)
  public:
    explicit Workspace(QUrl server, QObject *parent = nullptr);
    QVariantMap data() const { return m_data; }
    QQmlPropertyMap *viewData() { return &m_viewData; }
    Q_INVOKABLE void ensure(const QString &key, const QString &path, int maxAgeMs = 15000);
    Q_INVOKABLE void invalidate(const QString &key = QString());
    Q_INVOKABLE QVariantMap diagnostics() const { return m_stats; }
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
    QVariantMap m_data, m_errors, m_busy, m_stats;
    QQmlPropertyMap m_viewData;
    QElapsedTimer m_clock;
    QHash<QString, qint64> m_fetchedAt;
    QHash<QString, QString> m_paths;
    QHash<QString, QString> m_requestedPaths;
    QHash<QString, qint64> m_keyEpoch;
    qint64 m_epoch = 0;
};
