#pragma once
#include "geometry.h"
#include <QColor>
#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QPointer>
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
    Q_PROPERTY(bool visible MEMBER visible NOTIFY poseChanged)
    Q_PROPERTY(QVector3D position MEMBER position NOTIFY poseChanged)
    Q_PROPERTY(QQuaternion rotation MEMBER rotation NOTIFY poseChanged)
    Q_PROPERTY(QUrl modelSource MEMBER modelSource NOTIFY modelChanged)
    Q_PROPERTY(float modelScale MEMBER modelScale NOTIFY modelChanged)
    Q_PROPERTY(QVector3D modelOffset MEMBER modelOffset NOTIFY modelChanged)
    Q_PROPERTY(QQuaternion modelRotation MEMBER modelRotation NOTIFY modelChanged)
    Q_PROPERTY(TrailGeometry *trail READ trail CONSTANT)
    Q_PROPERTY(bool trailEnabled MEMBER trailEnabled NOTIFY traceChanged)
    Q_PROPERTY(int pointCount READ pointCount NOTIFY traceChanged)
    Q_PROPERTY(double opening MEMBER opening NOTIFY openingChanged)
  public:
    explicit Pose(QObject *parent = nullptr) : QObject(parent), geometry(new TrailGeometry) {}
    ~Pose() override { delete geometry; }
    QString name, role, assetKey;
    QColor color;
    bool visible = false, trailEnabled = true;
    bool geometryDirty = true;
    bool keepMode = false;
    QVector3D position, modelOffset;
    QQuaternion rotation, modelRotation;
    QUrl modelSource;
    float modelScale = 20;
    double opening = 1;
    QVector<QVector3D> points, kept;
    qint64 generation = -1, firstSeq = 1, lastSeq = 0, lastGeometry = 0;
    TrailGeometry *geometry;
    TrailGeometry *trail() const { return geometry; }
    int pointCount() const { return keepMode ? kept.size() : points.size(); }
    void notify() {
        emit poseChanged();
        emit modelChanged();
        emit traceChanged();
        emit openingChanged();
    }
    Q_INVOKABLE void setTrail(bool enabled) {
        trailEnabled = enabled;
        geometryDirty = true;
        emit traceChanged();
    }
  signals:
    void poseChanged();
    void modelChanged();
    void traceChanged();
    void openingChanged();
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
    Q_PROPERTY(QVariantMap cameraAvailability READ cameraAvailability NOTIFY availabilityChanged)
    Q_PROPERTY(QVariantMap localization READ localization NOTIFY localizationChanged)
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
    QVariantMap cameraAvailability() const { return m_cameraAvailability; }
    QVariantMap localization() const { return m_localization; }
    void start();
    void setKeepTrail(bool value);
    Q_INVOKABLE void command(const QString &action, const QString &value = QString());
    Q_INVOKABLE void setPlaybackTime(double seconds);
    Q_INVOKABLE void updateGripper(QObject *root, double opening);
    Q_INVOKABLE void resetGripper(QObject *root);
    Q_INVOKABLE QVariantMap diagnostics() const;
    void shutdown();
  signals:
    void tasksChanged();
    void bagsChanged();
    void camerasChanged();
    void posesChanged();
    void stateChanged();
    void availabilityChanged();
    void localizationChanged();

  private:
    void fetch(const QString &path, std::function<void(QJsonObject)> done);
    void post(const QString &path, const QJsonObject &body, std::function<void(QJsonObject)> done = {});
    void request(const QString &path, const QJsonObject *body, std::function<void(QJsonObject)> done);
    void poll();
    void updateLocalization();
    static QVariantMap localizationSummary(const QVariantMap &status, bool imageOnline,
                                           bool vioFresh, bool head);
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
    QVariantList m_poseTrace;
    QVariantMap m_state;
    QVariantMap m_cameraAvailability;
    QVariantMap m_localization, m_nativeVioFresh;
    QHash<QObject *, QList<QPointer<QObject>>> m_gripperNodes;
    QHash<QObject *, double> m_gripperOpenings;
    QString m_message = "正在连接后端", m_requestedPlayback;
    QJsonObject m_manifest, m_latestLive;
    QSet<QString> m_pending;
    bool m_connected = false, m_keep = false, m_haveOrigin = false, m_stopping = false,
         m_playbackStarting = false;
    QVector3D m_origin;
    qint64 m_lastPose = 0;
    qint64 m_lastCameraUpdate = -1, m_lastMappingUpdate = -1;
    int m_pollCount = 0;
};
