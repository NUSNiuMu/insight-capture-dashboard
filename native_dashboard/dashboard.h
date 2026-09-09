#pragma once
#include "geometry.h"
#include <QColor>
#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QObject>
#include <QQuaternion>
#include <QSet>
#include <QTimer>
#include <QVariantList>
#include <QWebSocket>
#include <functional>

class Pose : public QObject {
    Q_OBJECT
    Q_PROPERTY(QString name MEMBER name CONSTANT)
    Q_PROPERTY(QString role MEMBER role CONSTANT)
    Q_PROPERTY(QColor color MEMBER color CONSTANT)
    Q_PROPERTY(bool visible MEMBER visible NOTIFY changed)
    Q_PROPERTY(QVector3D position MEMBER position NOTIFY changed)
    Q_PROPERTY(QQuaternion rotation MEMBER rotation NOTIFY changed)
    Q_PROPERTY(QUrl modelSource MEMBER modelSource NOTIFY changed)
    Q_PROPERTY(float modelScale MEMBER modelScale NOTIFY changed)
    Q_PROPERTY(QVector3D modelOffset MEMBER modelOffset NOTIFY changed)
    Q_PROPERTY(QQuaternion modelRotation MEMBER modelRotation NOTIFY changed)
    Q_PROPERTY(TrailGeometry *trail READ trail CONSTANT)
    Q_PROPERTY(bool trailEnabled MEMBER trailEnabled NOTIFY changed)
    Q_PROPERTY(int pointCount READ pointCount NOTIFY changed)
    Q_PROPERTY(double opening MEMBER opening NOTIFY changed)
  public:
    explicit Pose(QObject *parent = nullptr) : QObject(parent), geometry(new TrailGeometry) {}
    ~Pose() override { delete geometry; }
    QString name, role, assetKey;
    QColor color;
    bool visible = false, trailEnabled = true;
    QVector3D position, modelOffset;
    QQuaternion rotation, modelRotation;
    QUrl modelSource;
    float modelScale = 20;
    double opening = 1;
    QVector<QVector3D> points, kept;
    qint64 generation = -1, firstSeq = 1, lastSeq = 0, lastGeometry = 0;
    TrailGeometry *geometry;
    TrailGeometry *trail() const { return geometry; }
    int pointCount() const { return points.size(); }
    void notify() { emit changed(); }
    Q_INVOKABLE void setTrail(bool enabled) {
        trailEnabled = enabled;
        emit changed();
    }
  signals:
    void changed();
};

class Dashboard : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList cameras READ cameras NOTIFY camerasChanged)
    Q_PROPERTY(QVariantList poses READ poses NOTIFY posesChanged)
    Q_PROPERTY(QVariantList tasks READ tasks NOTIFY tasksChanged)
    Q_PROPERTY(QVariantList bags READ bags NOTIFY bagsChanged)
    Q_PROPERTY(QVariantMap state READ state NOTIFY stateChanged)
    Q_PROPERTY(QString message READ message NOTIFY stateChanged)
    Q_PROPERTY(bool connected READ connected NOTIFY stateChanged)
    Q_PROPERTY(bool keepTrail READ keepTrail WRITE setKeepTrail NOTIFY stateChanged)
    Q_PROPERTY(bool playback READ playback NOTIFY camerasChanged)
    Q_PROPERTY(double duration READ duration NOTIFY camerasChanged)
    Q_PROPERTY(int targetFps READ targetFps CONSTANT)
  public:
    Dashboard(QUrl server, int fps, QObject *parent = nullptr);
    QVariantList cameras() const { return m_cameras; }
    QVariantList poses() const;
    QVariantList tasks() const { return m_tasks; }
    QVariantList bags() const { return m_bags; }
    QVariantMap state() const { return m_state; }
    QString message() const { return m_message; }
    bool connected() const { return m_connected; }
    bool keepTrail() const { return m_keep; }
    bool playback() const { return !m_manifest.isEmpty(); }
    double duration() const { return m_manifest["duration_s"].toDouble(); }
    int targetFps() const { return m_fps; }
    void start();
    void setKeepTrail(bool value);
    Q_INVOKABLE void command(const QString &action, const QString &value = QString());
    Q_INVOKABLE void setPlaybackTime(double seconds);
    Q_INVOKABLE void updateGripper(QObject *root, double opening);
    Q_INVOKABLE QVariantMap diagnostics() const;
    void shutdown();
  signals:
    void tasksChanged();
    void bagsChanged();
    void camerasChanged();
    void posesChanged();
    void stateChanged();

  private:
    void fetch(const QString &path, std::function<void(QJsonObject)> done);
    void post(const QString &path, const QJsonObject &body, std::function<void(QJsonObject)> done = {});
    void request(const QString &path, const QJsonObject *body, std::function<void(QJsonObject)> done);
    void poll();
    void refreshBags();
    void connectPose();
    void acceptPose(const QJsonObject &payload);
    void cacheModel(Pose *pose, const QJsonObject &data);
    void clearTraces();
    void goLive();
    void activatePlayback(const QJsonObject &status);
    QVector3D mapPoint(const QJsonArray &point) const;
    QUrl m_server;
    int m_fps;
    QNetworkAccessManager m_http;
    QWebSocket m_poseSocket;
    QTimer m_poll, m_reconnect;
    QElapsedTimer m_clock;
    QHash<QString, Pose *> m_poseByName;
    QList<Pose *> m_poseOrder;
    QVariantList m_cameras, m_liveCameras, m_tasks, m_bags;
    QVariantMap m_state;
    QString m_message = "正在连接后端", m_requestedPlayback;
    QJsonObject m_manifest, m_latestLive;
    QSet<QString> m_pending;
    bool m_connected = false, m_keep = false, m_haveOrigin = false, m_stopping = false,
         m_playbackStarting = false;
    QVector3D m_origin;
    qint64 m_lastPose = 0;
    int m_pollCount = 0;
};
