#pragma once
#include <QElapsedTimer>
#include <QPointer>
#include <QQuickItem>
#include <QTimer>
#include <QWebSocket>
#include <QWindow>
#include <atomic>
#include <gst/gst.h>
#include <gst/webrtc/webrtc.h>

class NativeVideo : public QQuickItem {
    Q_OBJECT
    Q_PROPERTY(QString cameraName READ cameraName WRITE setCameraName NOTIFY sourceChanged)
    Q_PROPERTY(QUrl endpoint READ endpoint WRITE setEndpoint NOTIFY sourceChanged)
    Q_PROPERTY(QUrl media READ media WRITE setMedia NOTIFY sourceChanged)
    Q_PROPERTY(int targetFps READ targetFps WRITE setTargetFps NOTIFY sourceChanged)
    Q_PROPERTY(QString status READ status NOTIFY statsChanged)
    Q_PROPERTY(double renderedFps READ renderedFps NOTIFY statsChanged)
    Q_PROPERTY(double position READ position NOTIFY statsChanged)
    Q_PROPERTY(bool paused READ paused WRITE setPaused NOTIFY statsChanged)
  public:
    explicit NativeVideo(QQuickItem *parent = nullptr);
    ~NativeVideo() override;
    QString cameraName() const { return m_name; }
    QUrl endpoint() const { return m_endpoint; }
    QUrl media() const { return m_media; }
    int targetFps() const { return m_fps; }
    QString status() const { return m_status; }
    double renderedFps() const { return m_renderedFps; }
    double position() const { return m_position; }
    bool paused() const { return m_paused; }
    void setCameraName(const QString &value);
    void setEndpoint(const QUrl &value);
    void setMedia(const QUrl &value);
    void setTargetFps(int value);
    void setPaused(bool value);
    Q_INVOKABLE void seek(double seconds);
    Q_INVOKABLE void reconnect();
    Q_INVOKABLE QVariantMap diagnostics() const;

  protected:
    void componentComplete() override;
    void geometryChange(const QRectF &, const QRectF &) override;
  signals:
    void sourceChanged();
    void statsChanged();

  private:
    void syncWindow();
    void start();
    void stop();
    void poll();
    void handleMessage(const QString &text);
    void fail(const QString &message);
    void send(const QJsonObject &message);
    static void incomingPad(GstElement *, GstPad *, gpointer);
    static void iceCandidate(GstElement *, guint, gchar *, gpointer);
    static void answerCreated(GstPromise *, gpointer);
    QString m_name, m_status = "未连接";
    QUrl m_endpoint, m_media;
    QPointer<QWindow> m_surface;
    QWebSocket m_socket;
    QTimer m_timer, m_restart;
    QElapsedTimer m_clock;
    GstElement *m_pipeline = nullptr, *m_peer = nullptr, *m_decode = nullptr, *m_sink = nullptr;
    GstBus *m_bus = nullptr;
    bool m_complete = false, m_stopping = false, m_paused = false;
    int m_fps = 25;
    std::atomic<int> m_generation{0};
    quint64 m_lastRendered = 0, m_rendered = 0, m_dropped = 0;
    qint64 m_lastPoll = 0, m_lastProgress = 0;
    double m_renderedFps = 0, m_position = 0;
};
