#include "video.h"
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPointer>
#include <QQuickWindow>
#include <QUrlQuery>
#include <QtEndian>
#include <algorithm>
#include <gst/sdp/sdp.h>
#include <gst/video/video-info.h>
#include <gst/video/videooverlay.h>

struct AnswerContext {
    QPointer<NativeVideo> video;
    int generation;
    GstElement *peer;
};
NativeVideo::NativeVideo(QQuickItem *parent) : QQuickItem(parent) {
    m_traceFrames = qEnvironmentVariable("INSIGHT_LATENCY_TRACE") == "1";
    m_clock.start();
    connect(QCoreApplication::instance(), &QCoreApplication::aboutToQuit, this, &NativeVideo::stop);
    m_restart.setSingleShot(true);
    connect(&m_restart, &QTimer::timeout, this, &NativeVideo::start);
    connect(&m_timer, &QTimer::timeout, this, &NativeVideo::poll);
    connect(&m_socket, &QWebSocket::textMessageReceived, this, &NativeVideo::handleMessage);
    connect(&m_socket, &QWebSocket::binaryMessageReceived, this, &NativeVideo::handleNativeFrame);
    m_socket.setMaxAllowedIncomingMessageSize(16 * 1024 * 1024);
    connect(&m_socket, QOverload<QAbstractSocket::SocketError>::of(&QWebSocket::error), this,
            [this](QAbstractSocket::SocketError) {
                if (!m_stopping && m_media.isEmpty())
                    fail(m_socket.errorString());
            });
    connect(&m_socket, &QWebSocket::disconnected, this, [this] {
        if (!m_stopping && m_media.isEmpty())
            fail("信令断开，重连中");
    });
    connect(this, &QQuickItem::windowChanged, this, [this] { syncWindow(); });
    connect(this, &QQuickItem::visibleChanged, this, &NativeVideo::syncWindow);
}
NativeVideo::~NativeVideo() {
    stop();
    delete m_surface.data();
}
void NativeVideo::setCameraName(const QString &v) {
    if (v == m_name)
        return;
    m_name = v;
    emit sourceChanged();
    reconnect();
}
void NativeVideo::setEndpoint(const QUrl &v) {
    if (v == m_endpoint)
        return;
    m_endpoint = v;
    emit sourceChanged();
    reconnect();
}
void NativeVideo::setNativeEndpoint(const QUrl &value) {
    if (value == m_nativeEndpoint)
        return;
    m_nativeEndpoint = value;
    emit sourceChanged();
    reconnect();
}
void NativeVideo::setMedia(const QUrl &v) {
    if (v == m_media)
        return;
    m_media = v;
    emit sourceChanged();
    reconnect();
}
void NativeVideo::setSourceSize(const QSize &value) {
    if (value == m_sourceSize)
        return;
    m_sourceSize = value;
    emit sourceChanged();
    reconnect();
}
void NativeVideo::setSourceOnline(bool value) {
    if (value == m_sourceOnline)
        return;
    m_sourceOnline = value;
    emit sourceChanged();
    if (!value && m_media.isEmpty()) {
        stop();
        m_status = "设备离线，等待连接";
        syncWindow();
        emit statsChanged();
    } else
        reconnect();
}
void NativeVideo::setTargetFps(int v) {
    v = qBound(1, v, 30);
    if (v == m_fps)
        return;
    m_fps = v;
    emit sourceChanged();
    reconnect();
}
void NativeVideo::componentComplete() {
    QQuickItem::componentComplete();
    m_complete = true;
    syncWindow();
    reconnect();
}
void NativeVideo::geometryChange(const QRectF &n, const QRectF &o) {
    QQuickItem::geometryChange(n, o);
    syncWindow();
}
void NativeVideo::syncWindow() {
    if (!m_complete || !window())
        return;
    if (!m_surface) {
        m_surface = new QWindow(window());
        m_surface->setFlags(Qt::SubWindow | Qt::FramelessWindowHint);
        m_surface->setSurfaceType(QSurface::OpenGLSurface);
        m_surface->create();
        connect(window(), &QWindow::widthChanged, this, [this] { syncWindow(); });
        connect(window(), &QWindow::heightChanged, this, [this] { syncWindow(); });
        connect(window(), &QWindow::visibleChanged, this, [this] { syncWindow(); });
    }
    const QPointF pos = mapToScene(QPointF());
    QSizeF size(width(), height());
    if (m_sink) {
        auto *pad = gst_element_get_static_pad(m_sink, "sink");
        auto *caps = gst_pad_get_current_caps(pad);
        if (caps) {
            GstVideoInfo info;
            if (gst_video_info_from_caps(&info, caps)) {
                m_displaySize = QSize(info.width, info.height);
                size = QSizeF(info.width, info.height).scaled(size, Qt::KeepAspectRatio);
            }
            gst_caps_unref(caps);
        }
        gst_object_unref(pad);
    }
    m_surface->setGeometry(qRound(pos.x() + (width() - size.width()) / 2),
                           qRound(pos.y() + (height() - size.height()) / 2), qMax(1, qRound(size.width())),
                           qMax(1, qRound(size.height())));
    m_surface->setVisible(isVisible() && (m_sourceOnline || !m_media.isEmpty()) &&
                          window()->isVisible() && width() > 1 && height() > 1);
}
void NativeVideo::reconnect() {
    if (m_complete && (m_sourceOnline || !m_media.isEmpty()))
        m_restart.start(150);
}
void NativeVideo::stop() {
    m_stopping = true;
    ++m_generation;
    m_restart.stop();
    m_timer.stop();
    m_socket.abort();
    if (m_pipeline) {
        if (m_peer)
            g_signal_handlers_disconnect_by_data(m_peer, this);
        gst_element_set_state(m_pipeline, GST_STATE_NULL);
        if (m_bus)
            gst_object_unref(m_bus);
        if (m_sink)
            gst_object_unref(m_sink);
        if (m_peer)
            gst_object_unref(m_peer);
        if (m_appsrc)
            gst_object_unref(m_appsrc);
        gst_object_unref(m_pipeline);
    }
    m_pipeline = m_peer = m_decode = m_sink = nullptr;
    m_appsrc = nullptr;
    m_nativeReceived = 0;
    m_nativeFormat = 0;
    m_bus = nullptr;
    m_renderedFps = 0;
    m_rendered = m_dropped = m_lastRendered = 0;
    m_latestFrameDrops = 0;
    m_boundedJitterBuffers = 0;
    {
        QMutexLocker lock(&m_timingMutex);
        m_decodeStarts.clear();
        m_decodeTimes.clear();
        m_frameTrace.clear();
        m_timingIndex = 0;
    }
    m_stopping = false;
}
void NativeVideo::start() {
    stop();
    syncWindow();
    if (!m_surface || m_name.isEmpty() || m_endpoint.isEmpty() || (!m_sourceOnline && m_media.isEmpty()))
        return;
    m_useNative = m_media.isEmpty() && !m_nativeEndpoint.isEmpty();
    if (m_useNative) {
        m_status = "正在连接本机视频";
        m_lastProgress = m_clock.elapsed();
        QUrl url = m_nativeEndpoint;
        QUrlQuery query;
        query.addQueryItem("camera", m_name);
        query.addQueryItem("fps", QString::number(m_fps));
        url.setQuery(query);
        m_socket.open(url);
        emit statsChanged();
        return;
    }
    GError *error = nullptr;
    const auto sinkName = qEnvironmentVariable("INSIGHT_NATIVE_VIDEO_SINK") == "nveglglessink"
                              ? QByteArray("nveglglessink")
                              : QByteArray("nv3dsink");
    // Live H.264 has no B frames; DPB buffering otherwise adds hundreds of milliseconds.
    // Drop only decoded frames here: dropping arbitrary H.264 packets breaks reference frames.
    const char *decode = "queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 ! "
                         "rtph264depay request-keyframe=true wait-for-keyframe=true ! "
                         "video/x-h264,alignment=au ! "
                         "h264parse ! nvv4l2decoder name=hardwareDecoder disable-dpb=true enable-max-performance=true ! "
                         "queue name=latestFrame max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                         "nv3dsink name=display sync=false qos=false enable-last-sample=false";
    if (m_media.isEmpty()) {
        m_pipeline = gst_pipeline_new(nullptr);
        auto *peer = gst_element_factory_make("webrtcbin", "peer");
        QByteArray liveDecode(decode);
        const int longest = qMax(m_sourceSize.width(), m_sourceSize.height());
        if (longest > 0) {
            const double factor = qMin(1.0, 720.0 / longest);
            const int width = qMax(2, int(m_sourceSize.width() * factor) / 2 * 2);
            const int height = qMax(2, int(m_sourceSize.height() * factor) / 2 * 2);
            // I420 forces a separate VIC surface even when no resize is needed.
            const auto scaling = QString("nvvidconv ! video/x-raw(memory:NVMM),format=I420,width=%1,height=%2 ! "
                                         "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! ")
                                     .arg(width).arg(height).toUtf8();
            liveDecode.replace("nv3dsink name=display", scaling + "nv3dsink name=display");
        }
        m_decode = gst_parse_bin_from_description(
            liveDecode.replace("nv3dsink", sinkName).constData(), TRUE, &error);
        if (!peer || !m_decode || error) {
            if (peer)
                gst_object_unref(peer);
            if (m_decode)
                gst_object_unref(m_decode);
            m_decode = nullptr;
            fail(error ? QString::fromUtf8(error->message) : "缺少 WebRTC/NVIDIA 插件");
            if (error)
                g_error_free(error);
            return;
        }
        g_object_set(peer, "bundle-policy", GST_WEBRTC_BUNDLE_POLICY_MAX_BUNDLE, "latency", 50, nullptr);
        g_signal_connect(peer, "deep-element-added", G_CALLBACK(boundJitterBuffer), this);
        gst_bin_add_many(GST_BIN(m_pipeline), peer, m_decode, nullptr);
        m_peer = GST_ELEMENT(gst_object_ref(peer));
        g_signal_connect(peer, "pad-added", G_CALLBACK(incomingPad), this);
        g_signal_connect(peer, "on-ice-candidate", G_CALLBACK(iceCandidate), this);
        auto *latest = gst_bin_get_by_name(GST_BIN(m_decode), "latestFrame");
        g_signal_connect(latest, "overrun", G_CALLBACK(latestFrameOverrun), this);
        gst_object_unref(latest);
    } else {
        m_pipeline = gst_parse_launch(
            QByteArray("uridecodebin name=input caps=video/x-h264 ! queue ! h264parse config-interval=-1 ! "
                       "video/x-h264,stream-format=byte-stream,alignment=au ! nvv4l2decoder "
                       "name=hardwareDecoder enable-max-performance=true ! queue max-size-buffers=4 "
                       "max-size-bytes=0 max-size-time=0 ! nv3dsink name=display sync=true "
                       "qos=true max-lateness=50000000 enable-last-sample=false")
                .replace("nv3dsink", sinkName)
                .constData(),
            &error);
        if (!m_pipeline || error) {
            fail(error ? QString::fromUtf8(error->message) : "回放管线创建失败");
            if (error)
                g_error_free(error);
            return;
        }
        auto *input = gst_bin_get_by_name(GST_BIN(m_pipeline), "input");
        g_object_set(input, "uri", m_media.toEncoded().constData(), nullptr);
        gst_object_unref(input);
    }
    startPipeline();
}

void NativeVideo::startPipeline() {
    auto *decoder = gst_bin_get_by_name(GST_BIN(m_pipeline), "hardwareDecoder");
    if (decoder) {
        if (m_media.isEmpty() && qEnvironmentVariable("INSIGHT_NATIVE_DECODER_DPB") == "1")
            g_object_set(decoder, "disable-dpb", FALSE, nullptr);
        if (m_media.isEmpty()) {
            for (const char *name : {"sink", "src"}) {
                auto *pad = gst_element_get_static_pad(decoder, name);
                gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER, decodeTiming, this, nullptr);
                gst_object_unref(pad);
            }
        }
        gst_object_unref(decoder);
    }
    m_sink = gst_bin_get_by_name(GST_BIN(m_pipeline), "display");
    if (m_sink && m_traceFrames && m_media.isEmpty()) {
        auto *pad = gst_element_get_static_pad(m_sink, "sink");
        gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_BUFFER, sinkTiming, this, nullptr);
        gst_object_unref(pad);
    }
    if (!m_sink || !GST_IS_VIDEO_OVERLAY(m_sink)) {
        fail("视频 sink 不支持原生窗口");
        return;
    }
    m_bus = gst_element_get_bus(m_pipeline);
    // nv3dsink initializes its display only before prepare-window-handle.
    const auto handle = guintptr(m_surface->winId());
    gst_bus_set_sync_handler(
        m_bus,
        [](GstBus *, GstMessage *message, gpointer data) {
            if (!gst_is_video_overlay_prepare_window_handle_message(message))
                return GST_BUS_PASS;
            gst_video_overlay_set_window_handle(GST_VIDEO_OVERLAY(GST_MESSAGE_SRC(message)), guintptr(data));
            gst_message_unref(message);
            return GST_BUS_DROP;
        },
        reinterpret_cast<gpointer>(handle), nullptr);
    m_status = m_useNative ? "本机直通 · 原生显示" : m_media.isEmpty() ? "正在协商 H.264" : "正在加载回放";
    m_lastPoll = m_lastProgress = m_clock.elapsed();
    if (gst_element_set_state(m_pipeline, m_paused ? GST_STATE_PAUSED : GST_STATE_PLAYING) ==
        GST_STATE_CHANGE_FAILURE) {
        fail("视频管线启动失败");
        return;
    }
    m_timer.start(33);
    if (m_media.isEmpty() && !m_useNative) {
        QUrl url = m_endpoint;
        url.setPath("/ws/webrtc");
        QUrlQuery query;
        query.addQueryItem("camera", m_name);
        query.addQueryItem("fps", QString::number(m_fps));
        url.setQuery(query);
        m_socket.open(url);
    }
    emit statsChanged();
}

void NativeVideo::startNativePipeline(int format, int width, int height) {
    const auto sink = qEnvironmentVariable("INSIGHT_NATIVE_VIDEO_SINK") == "nveglglessink"
                          ? QByteArray("nveglglessink") : QByteArray("nv3dsink");
    QByteArray source;
    if (format == 4)
        source = "appsrc name=src is-live=true format=time block=false max-buffers=8 max-bytes=0 "
                 "caps=video/x-h264,stream-format=byte-stream,alignment=au ! h264parse ! "
                 "nvv4l2decoder name=hardwareDecoder disable-dpb=true enable-max-performance=true ! ";
    else if (format == 3)
        source = "appsrc name=src is-live=true format=time block=false max-buffers=1 max-bytes=0 leaky-type=downstream "
                 "caps=image/jpeg ! nvjpegdec ! ";
    else
        source = QString("appsrc name=src is-live=true format=time block=false max-buffers=1 max-bytes=0 leaky-type=downstream "
                         "caps=video/x-raw,format=%1,width=%2,height=%3,framerate=%4/1 ! ")
                     .arg(format == 1 ? "GRAY8" : "NV12").arg(width).arg(height).arg(m_fps).toUtf8();
    const double factor = qMin(1.0, 720.0 / qMax(width, height));
    const int outWidth = qMax(2, int(width * factor) / 2 * 2);
    const int outHeight = qMax(2, int(height * factor) / 2 * 2);
    // VIC conversion also releases decoder surfaces before the display retains them.
    const auto display = QString("queue name=latestFrame max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                                 "nvvidconv ! video/x-raw(memory:NVMM),format=I420,width=%1,height=%2 ! "
                                 "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
                                 "%3 name=display sync=false qos=false enable-last-sample=false")
                             .arg(outWidth).arg(outHeight).arg(QString::fromUtf8(sink)).toUtf8();
    GError *error = nullptr;
    m_pipeline = gst_parse_launch((source + display).constData(), &error);
    if (!m_pipeline || error) {
        fail(error ? QString::fromUtf8(error->message) : "本机视频管线创建失败");
        if (error)
            g_error_free(error);
        return;
    }
    m_nativeFormat = format;
    m_nativeSize = QSize(width, height);
    m_appsrc = gst_bin_get_by_name(GST_BIN(m_pipeline), "src");
    auto *latest = gst_bin_get_by_name(GST_BIN(m_pipeline), "latestFrame");
    g_signal_connect(latest, "overrun", G_CALLBACK(latestFrameOverrun), this);
    gst_object_unref(latest);
    m_nativeEpoch = gst_util_get_timestamp();
    startPipeline();
}

void NativeVideo::handleNativeFrame(const QByteArray &message) {
    if (!m_useNative || !m_media.isEmpty() || message.size() <= 32)
        return;
    const auto *header = reinterpret_cast<const uchar *>(message.constData());
    const int format = header[5];
    const auto width = qFromBigEndian<quint32>(header + 8);
    const auto height = qFromBigEndian<quint32>(header + 12);
    if (message.left(4) != "INPV" || header[4] != 1 || format < 1 || format > 4 ||
        !width || !height || width > 8192 || height > 8192 ||
        (format == 1 && message.size() - 32 != qint64(width) * height) ||
        (format == 2 && (height % 2 || message.size() - 32 != qint64(width) * height * 3 / 2))) {
        fail("本机视频数据格式错误");
        return;
    }
    if (!m_pipeline)
        startNativePipeline(format, width, height);
    if (!m_appsrc)
        return;
    if (format != m_nativeFormat || QSize(width, height) != m_nativeSize) {
        m_restart.start(0);
        return;
    }
    if (format == 4 && gst_app_src_get_current_level_buffers(GST_APP_SRC(m_appsrc)) >= 8) {
        // Reconnect at a complete IDR instead of dropping dependent H.264 pictures.
        m_restart.start(0);
        return;
    }
    const auto now = gst_util_get_timestamp();
    const auto received = qFromBigEndian<quint64>(header + 24);
    if (now > received && now - received > 150 * GST_MSECOND) {
        if (format == 4)
            m_restart.start(0);
        return;
    }
    auto *owned = new QByteArray(message);
    auto *buffer = gst_buffer_new_wrapped_full(GST_MEMORY_FLAG_READONLY, const_cast<char *>(owned->constData() + 32),
                                               owned->size() - 32, 0, owned->size() - 32, owned,
                                               [](gpointer data) { delete static_cast<QByteArray *>(data); });
    const quint64 pts = now - m_nativeEpoch;
    GST_BUFFER_PTS(buffer) = pts;
    GST_BUFFER_DTS(buffer) = pts;
    GST_BUFFER_DURATION(buffer) = GST_SECOND / m_fps;
    if (m_traceFrames) {
        QMutexLocker lock(&m_timingMutex);
        m_frameTrace[pts] = {{"source_stamp_ns", qulonglong(qFromBigEndian<quint64>(header + 16))},
                             {"received_ns", qulonglong(received)}, {"native_input_ns", qulonglong(now)}};
        while (m_frameTrace.size() > 120)
            m_frameTrace.erase(m_frameTrace.begin());
    }
    ++m_nativeReceived;
    if (gst_app_src_push_buffer(GST_APP_SRC(m_appsrc), buffer) != GST_FLOW_OK)
        fail("本机视频投递失败");
}
void NativeVideo::fail(const QString &message) {
    if (m_stopping)
        return;
    m_status = message;
    m_renderedFps = 0;
    emit statsChanged();
    if (!m_restart.isActive())
        m_restart.start(3000);
}
void NativeVideo::send(const QJsonObject &message) {
    if (m_socket.state() == QAbstractSocket::ConnectedState)
        m_socket.sendTextMessage(QString::fromUtf8(QJsonDocument(message).toJson(QJsonDocument::Compact)));
}
void NativeVideo::handleMessage(const QString &text) {
    if (!m_peer)
        return;
    auto data = QJsonDocument::fromJson(text.toUtf8()).object();
    if (data["type"] == "offer") {
        GstSDPMessage *sdp = nullptr;
        gst_sdp_message_new(&sdp);
        auto bytes = data["sdp"].toString().toUtf8();
        if (gst_sdp_message_parse_buffer(reinterpret_cast<const guint8 *>(bytes.constData()), bytes.size(),
                                         sdp) != GST_SDP_OK) {
            gst_sdp_message_free(sdp);
            fail("SDP 解析失败");
            return;
        }
        auto *offer = gst_webrtc_session_description_new(GST_WEBRTC_SDP_TYPE_OFFER, sdp);
        g_signal_emit_by_name(m_peer, "set-remote-description", offer, nullptr);
        gst_webrtc_session_description_free(offer);
        auto *context = new AnswerContext{this, m_generation, GST_ELEMENT(gst_object_ref(m_peer))};
        auto *promise = gst_promise_new_with_change_func(answerCreated, context, nullptr);
        g_signal_emit_by_name(m_peer, "create-answer", nullptr, promise);
    } else if (data["type"] == "ice") {
        auto candidate = data["candidate"].toString().toUtf8();
        g_signal_emit_by_name(m_peer, "add-ice-candidate", data["sdpMLineIndex"].toInt(),
                              candidate.constData());
    } else if (data["type"] == "webrtc_unavailable")
        fail("服务端 WebRTC 不可用");
}
void NativeVideo::answerCreated(GstPromise *promise, gpointer user) {
    auto *ctx = static_cast<AnswerContext *>(user);
    GstWebRTCSessionDescription *answer = nullptr;
    const auto *reply = gst_promise_get_reply(promise);
    if (reply)
        gst_structure_get(reply, "answer", GST_TYPE_WEBRTC_SESSION_DESCRIPTION, &answer, nullptr);
    QString sdp;
    if (answer) {
        g_signal_emit_by_name(ctx->peer, "set-local-description", answer, nullptr);
        gchar *text = gst_sdp_message_as_text(answer->sdp);
        sdp = QString::fromUtf8(text);
        g_free(text);
        gst_webrtc_session_description_free(answer);
    }
    if (ctx->video) {
        auto video = ctx->video;
        int generation = ctx->generation;
        QMetaObject::invokeMethod(
            video,
            [video, generation, sdp] {
                if (!video || video->m_generation != generation)
                    return;
                if (sdp.isEmpty())
                    video->fail("无法生成 WebRTC 应答");
                else
                    video->send({{"type", "answer"}, {"sdp", sdp}});
            },
            Qt::QueuedConnection);
    }
    gst_object_unref(ctx->peer);
    delete ctx;
    gst_promise_unref(promise);
}
void NativeVideo::incomingPad(GstElement *, GstPad *pad, gpointer user) {
    auto *self = static_cast<NativeVideo *>(user);
    if (GST_PAD_DIRECTION(pad) != GST_PAD_SRC || !self->m_decode)
        return;
    auto *sink = gst_element_get_static_pad(self->m_decode, "sink");
    if (!gst_pad_is_linked(sink))
        gst_pad_link(pad, sink);
    gst_object_unref(sink);
}
void NativeVideo::boundJitterBuffer(GstBin *, GstBin *, GstElement *element, gpointer user) {
    auto *factory = gst_element_get_factory(element);
    if (!factory || g_strcmp0(gst_plugin_feature_get_name(GST_PLUGIN_FEATURE(factory)), "rtpjitterbuffer"))
        return;
    // A latency target alone does not bound backlog when downstream is stalled.
    g_object_set(element, "latency", 50u, "drop-on-latency", TRUE, nullptr);
    ++static_cast<NativeVideo *>(user)->m_boundedJitterBuffers;
}
void NativeVideo::latestFrameOverrun(GstElement *, gpointer user) {
    ++static_cast<NativeVideo *>(user)->m_latestFrameDrops;
}
void NativeVideo::iceCandidate(GstElement *, guint index, gchar *candidate, gpointer user) {
    auto *self = static_cast<NativeVideo *>(user);
    QString text = QString::fromUtf8(candidate);
    int generation = self->m_generation;
    QMetaObject::invokeMethod(
        self,
        [self, index, text, generation] {
            if (self->m_generation == generation)
                self->send({{"type", "ice"}, {"candidate", text}, {"sdpMLineIndex", int(index)}});
        },
        Qt::QueuedConnection);
}
void NativeVideo::poll() {
    if (!m_pipeline)
        return;
    while (auto *message = gst_bus_pop(m_bus)) {
        if (GST_MESSAGE_TYPE(message) == GST_MESSAGE_ERROR) {
            GError *error = nullptr;
            gchar *debug = nullptr;
            gst_message_parse_error(message, &error, &debug);
            QString text = QString::fromUtf8(error->message);
            g_error_free(error);
            g_free(debug);
            gst_message_unref(message);
            fail(text);
            return;
        }
        if (GST_MESSAGE_TYPE(message) == GST_MESSAGE_EOS) {
            m_paused = true;
            m_status = "回放结束";
            emit statsChanged();
        }
        gst_message_unref(message);
    }
    auto now = m_clock.elapsed();
    if (!m_media.isEmpty()) {
        gint64 pos = 0;
        if (gst_element_query_position(m_pipeline, GST_FORMAT_TIME, &pos)) {
            m_position = double(pos) / GST_SECOND;
            emit statsChanged();
        }
    }
    if (now - m_lastPoll >= 1000 && m_sink) {
        GstStructure *stats = nullptr;
        g_object_get(m_sink, "stats", &stats, nullptr);
        guint64 rendered = 0, dropped = 0;
        if (stats) {
            gst_structure_get_uint64(stats, "rendered", &rendered);
            gst_structure_get_uint64(stats, "dropped", &dropped);
            gst_structure_free(stats);
        }
        m_renderedFps = (rendered >= m_lastRendered)
                            ? double(rendered - m_lastRendered) * 1000.0 / double(now - m_lastPoll)
                            : 0;
        if (rendered > m_lastRendered) {
            m_lastProgress = now;
            m_status = m_useNative ? "本机直通 · 原生显示" : "NVDEC · 原生显示";
        }
        m_rendered = rendered;
        m_dropped = dropped;
        m_lastRendered = rendered;
        m_lastPoll = now;
        syncWindow();
        emit statsChanged();
        if (m_media.isEmpty() && now - m_lastProgress > 12000)
            fail("视频停帧，重连中");
    }
}
void NativeVideo::setPaused(bool value) {
    m_paused = value;
    if (m_pipeline)
        gst_element_set_state(m_pipeline, value ? GST_STATE_PAUSED : GST_STATE_PLAYING);
    emit statsChanged();
}
void NativeVideo::seek(double seconds) {
    if (m_pipeline && !m_media.isEmpty())
        gst_element_seek_simple(m_pipeline, GST_FORMAT_TIME,
                                GstSeekFlags(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_KEY_UNIT),
                                qint64(qMax(0.0, seconds) * GST_SECOND));
}
static QString frameFingerprint(GstBuffer *buffer) {
    GstMapInfo map{};
    if (!gst_buffer_map(buffer, &map, GST_MAP_READ))
        return {};
    QString key;
    // h264parse emits byte-stream AUs; match the first VCL NAL independently of SPS/PPS.
    for (gsize i = 0; i + 4 < map.size; ++i) {
        if (map.data[i] || map.data[i + 1] || map.data[i + 2] != 1)
            continue;
        const gsize start = i + 3;
        const int kind = map.data[start] & 31;
        if (kind != 1 && kind != 5)
            continue;
        gsize end = map.size;
        for (gsize j = start + 1; j + 3 < map.size; ++j)
            if (!map.data[j] && !map.data[j + 1] && map.data[j + 2] == 1) {
                end = j;
                break;
            }
        while (end > start && !map.data[end - 1])
            --end;
        key = QString::fromLatin1(QCryptographicHash::hash(
            QByteArray(reinterpret_cast<const char *>(map.data + start), qMin<gsize>(64, end - start)),
            QCryptographicHash::Sha256).toHex().left(32));
        break;
    }
    gst_buffer_unmap(buffer, &map);
    return key;
}

GstPadProbeReturn NativeVideo::sinkTiming(GstPad *, GstPadProbeInfo *info, gpointer user) {
    auto *buffer = GST_PAD_PROBE_INFO_BUFFER(info);
    if (!buffer || !GST_BUFFER_PTS_IS_VALID(buffer))
        return GST_PAD_PROBE_OK;
    auto *self = static_cast<NativeVideo *>(user);
    const quint64 now = gst_util_get_timestamp();
    QMutexLocker lock(&self->m_timingMutex);
    auto it = self->m_frameTrace.find(GST_BUFFER_PTS(buffer));
    if (it != self->m_frameTrace.end())
        it.value()["sink_input_ns"] = qulonglong(now);
    return GST_PAD_PROBE_OK;
}

GstPadProbeReturn NativeVideo::decodeTiming(GstPad *pad, GstPadProbeInfo *info, gpointer user) {
    const auto *buffer = GST_PAD_PROBE_INFO_BUFFER(info);
    if (!buffer || !GST_BUFFER_PTS_IS_VALID(buffer))
        return GST_PAD_PROBE_OK;
    auto *self = static_cast<NativeVideo *>(user);
    const quint64 pts = GST_BUFFER_PTS(buffer), now = gst_util_get_timestamp();
    QMutexLocker lock(&self->m_timingMutex);
    if (GST_PAD_DIRECTION(pad) == GST_PAD_SINK) {
        if (self->m_traceFrames) {
            auto &trace = self->m_frameTrace[pts];
            trace["key"] = frameFingerprint(const_cast<GstBuffer *>(buffer));
            trace["decode_input_ns"] = qulonglong(now);
            while (self->m_frameTrace.size() > 120)
                self->m_frameTrace.erase(self->m_frameTrace.begin());
        }
        self->m_decodeStarts[pts] = now;
        while (self->m_decodeStarts.size() > 120)
            self->m_decodeStarts.erase(self->m_decodeStarts.begin());
    } else {
        if (self->m_traceFrames && self->m_frameTrace.contains(pts))
            self->m_frameTrace[pts]["decode_output_ns"] = qulonglong(now);
        auto it = self->m_decodeStarts.find(pts);
        if (it != self->m_decodeStarts.end()) {
            const double ms = double(now - it.value()) / GST_MSECOND;
            self->m_decodeStarts.erase(it);
            if (self->m_decodeTimes.size() < 120)
                self->m_decodeTimes.append(ms);
            else {
                self->m_decodeTimes[self->m_timingIndex] = ms;
                self->m_timingIndex = (self->m_timingIndex + 1) % 120;
            }
        }
    }
    return GST_PAD_PROBE_OK;
}
QVariantMap NativeVideo::diagnostics() const {
    QVector<double> timings;
    QVariantList frameTrace;
    {
        QMutexLocker lock(&m_timingMutex);
        timings = m_decodeTimes;
        for (const auto &frame : m_frameTrace)
            frameTrace.append(frame);
    }
    std::sort(timings.begin(), timings.end());
    return {{"camera", m_name},
            {"transport", !m_media.isEmpty() ? "replay" : m_useNative ? "local" : "webrtc"},
            {"native_received", qulonglong(m_nativeReceived)},
            {"frame_trace", frameTrace},
            {"display_width", m_displaySize.width()},
            {"display_height", m_displaySize.height()},
            {"decode_samples", timings.size()},
            {"decode_median_ms", timings.isEmpty() ? -1.0 : timings[timings.size() / 2]},
            {"decode_p95_ms", timings.isEmpty() ? -1.0 : timings[qMin(timings.size() - 1, timings.size() * 95 / 100)]},
            {"rendered", qulonglong(m_rendered)},
            {"dropped", qulonglong(m_dropped)},
            {"latest_frame_drops", qulonglong(m_latestFrameDrops.load())},
            {"bounded_jitter_buffers", m_boundedJitterBuffers.load()},
            {"sink_fps", m_renderedFps},
            {"status", m_status},
            {"media", m_media.toString()},
            {"sample_ms", m_lastPoll},
            {"position", m_position},
            {"paused", m_paused},
            {"generation", m_generation.load()}};
}
