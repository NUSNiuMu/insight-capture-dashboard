#pragma once
#include <QElapsedTimer>
#include <QMap>
#include <QMutex>
#include <QPointer>
#include <QQuickItem>
#include <QTimer>
#include <QWebSocket>
#include <QWindow>
#include <atomic>
#include <gst/gst.h>
#include <gst/app/gstappsrc.h>
#include <gst/webrtc/webrtc.h>

class NativeVideo : public QQuickItem {
    Q_OBJECT
    Q_PROPERTY(QString cameraName READ cameraName WRITE setCameraName NOTIFY sourceChanged)
    Q_PROPERTY(QUrl endpoint READ endpoint WRITE setEndpoint NOTIFY sourceChanged)
    Q_PROPERTY(QUrl nativeEndpoint READ nativeEndpoint WRITE setNativeEndpoint NOTIFY sourceChanged)
    Q_PROPERTY(QUrl media READ media WRITE setMedia NOTIFY sourceChanged)
    Q_PROPERTY(QSize sourceSize READ sourceSize WRITE setSourceSize NOTIFY sourceChanged)
    Q_PROPERTY(bool active READ active WRITE setActive NOTIFY sourceChanged)
    Q_PROPERTY(bool sourceOnline READ sourceOnline WRITE setSourceOnline NOTIFY sourceChanged)
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
    QUrl nativeEndpoint() const { return m_nativeEndpoint; }
    QUrl media() const { return m_media; }
    QSize sourceSize() const { return m_sourceSize; }
    bool active() const { return m_active; }
    void setActive(bool value);
    bool sourceOnline() const { return m_sourceOnline; }
    int targetFps() const { return m_fps; }
    QString status() const { return m_status; }
    double renderedFps() const { return m_renderedFps; }
    double position() const { return m_position; }
    bool paused() const { return m_paused; }
    void setCameraName(const QString &value);
    void setEndpoint(const QUrl &value);
    void setNativeEndpoint(const QUrl &value);
    void setMedia(const QUrl &value);
    void setTargetFps(int value);
    void setSourceSize(const QSize &value);
    void setSourceOnline(bool value);
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
    void handleNativeFrame(const QByteArray &data);
    void startNativePipeline(int format, int width, int height);
    void startPipeline();
    void fail(const QString &message);
    void send(const QJsonObject &message);
    static void incomingPad(GstElement *, GstPad *, gpointer);
    static void boundJitterBuffer(GstBin *, GstBin *, GstElement *, gpointer);
    static void latestFrameOverrun(GstElement *, gpointer);
    static void iceCandidate(GstElement *, guint, gchar *, gpointer);
    static void answerCreated(GstPromise *, gpointer);
    static GstPadProbeReturn decodeTiming(GstPad *, GstPadProbeInfo *, gpointer);
    static GstPadProbeReturn sinkTiming(GstPad *, GstPadProbeInfo *, gpointer);
    QString m_name, m_status = "未连接";
    QUrl m_endpoint, m_media, m_nativeEndpoint;
    QSize m_sourceSize, m_displaySize;
    QPointer<QWindow> m_surface;
    QWebSocket m_socket;
    QTimer m_timer, m_restart;
    QElapsedTimer m_clock;
    GstElement *m_pipeline = nullptr, *m_peer = nullptr, *m_decode = nullptr, *m_sink = nullptr;
    GstBus *m_bus = nullptr;
    GstElement *m_appsrc = nullptr;
    bool m_useNative = false;
    int m_nativeFormat = 0;
    QSize m_nativeSize;
    quint64 m_nativeEpoch = 0, m_nativeReceived = 0;
    std::atomic<bool> m_active{true};
    bool m_resumeFresh = false;
    quint64 m_resumeBaseline = 0;
    qint64 m_resumeStarted = 0;
    double m_resumeMs = -1;
    bool m_complete = false, m_stopping = false, m_paused = false, m_sourceOnline = true;
    int m_fps = 25;
    std::atomic<int> m_generation{0};
    std::atomic<quint64> m_latestFrameDrops{0};
    std::atomic<quint64> m_hiddenFrameDrops{0};
    quint64 m_hiddenRawSkips = 0;
    std::atomic<int> m_boundedJitterBuffers{0};
    quint64 m_lastRendered = 0, m_rendered = 0, m_dropped = 0;
    qint64 m_lastPoll = 0, m_lastProgress = 0;
    double m_renderedFps = 0, m_position = 0;
    mutable QMutex m_timingMutex;
    QMap<quint64, quint64> m_decodeStarts;
    QVector<double> m_decodeTimes;
    int m_timingIndex = 0;
    bool m_traceFrames = false;
    QMap<quint64, QVariantMap> m_frameTrace;
};
