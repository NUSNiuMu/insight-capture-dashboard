#include "video.h"
#include <QCoreApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPointer>
#include <QQuickWindow>
#include <QUrlQuery>
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
    m_clock.start();
    connect(QCoreApplication::instance(), &QCoreApplication::aboutToQuit, this, &NativeVideo::stop);
    m_restart.setSingleShot(true);
    connect(&m_restart, &QTimer::timeout, this, &NativeVideo::start);
    connect(&m_timer, &QTimer::timeout, this, &NativeVideo::poll);
    connect(&m_socket, &QWebSocket::textMessageReceived, this, &NativeVideo::handleMessage);
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
void NativeVideo::setMedia(const QUrl &v) {
    if (v == m_media)
        return;
    m_media = v;
    emit sourceChanged();
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
            if (gst_video_info_from_caps(&info, caps))
                size = QSizeF(info.width, info.height).scaled(size, Qt::KeepAspectRatio);
            gst_caps_unref(caps);
        }
        gst_object_unref(pad);
    }
    m_surface->setGeometry(qRound(pos.x() + (width() - size.width()) / 2),
                           qRound(pos.y() + (height() - size.height()) / 2), qMax(1, qRound(size.width())),
                           qMax(1, qRound(size.height())));
    m_surface->setVisible(isVisible() && window()->isVisible() && width() > 1 && height() > 1);
}
void NativeVideo::reconnect() {
    if (m_complete)
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
        gst_object_unref(m_pipeline);
    }
    m_pipeline = m_peer = m_decode = m_sink = nullptr;
    m_bus = nullptr;
    m_renderedFps = 0;
    m_rendered = m_dropped = m_lastRendered = 0;
    {
        QMutexLocker lock(&m_timingMutex);
        m_decodeStarts.clear();
        m_decodeTimes.clear();
        m_timingIndex = 0;
    }
    m_stopping = false;
}
void NativeVideo::start() {
    stop();
    syncWindow();
    if (!m_surface || m_name.isEmpty() || m_endpoint.isEmpty())
        return;
    GError *error = nullptr;
    const auto sinkName = qEnvironmentVariable("INSIGHT_NATIVE_VIDEO_SINK") == "nveglglessink"
                              ? QByteArray("nveglglessink")
                              : QByteArray("nv3dsink");
    // Live H.264 has no B frames; DPB buffering otherwise adds hundreds of milliseconds.
    const char *decode = "queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 ! rtph264depay ! "
                         "h264parse ! nvv4l2decoder name=hardwareDecoder disable-dpb=true enable-max-performance=true ! "
                         "nv3dsink name=display sync=false qos=false enable-last-sample=false";
    if (m_media.isEmpty()) {
        m_pipeline = gst_pipeline_new(nullptr);
        auto *peer = gst_element_factory_make("webrtcbin", "peer");
        m_decode = gst_parse_bin_from_description(
            QByteArray(decode).replace("nv3dsink", sinkName).constData(), TRUE, &error);
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
        gst_bin_add_many(GST_BIN(m_pipeline), peer, m_decode, nullptr);
        m_peer = GST_ELEMENT(gst_object_ref(peer));
        g_signal_connect(peer, "pad-added", G_CALLBACK(incomingPad), this);
        g_signal_connect(peer, "on-ice-candidate", G_CALLBACK(iceCandidate), this);
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
    m_status = m_media.isEmpty() ? "正在协商 H.264" : "正在加载回放";
    m_lastPoll = m_lastProgress = m_clock.elapsed();
    if (gst_element_set_state(m_pipeline, m_paused ? GST_STATE_PAUSED : GST_STATE_PLAYING) ==
        GST_STATE_CHANGE_FAILURE) {
        fail("视频管线启动失败");
        return;
    }
    m_timer.start(33);
    if (m_media.isEmpty()) {
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
            m_status = "NVDEC · 原生显示";
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
GstPadProbeReturn NativeVideo::decodeTiming(GstPad *pad, GstPadProbeInfo *info, gpointer user) {
    const auto *buffer = GST_PAD_PROBE_INFO_BUFFER(info);
    if (!buffer || !GST_BUFFER_PTS_IS_VALID(buffer))
        return GST_PAD_PROBE_OK;
    auto *self = static_cast<NativeVideo *>(user);
    const quint64 pts = GST_BUFFER_PTS(buffer), now = gst_util_get_timestamp();
    QMutexLocker lock(&self->m_timingMutex);
    if (GST_PAD_DIRECTION(pad) == GST_PAD_SINK) {
        self->m_decodeStarts[pts] = now;
        while (self->m_decodeStarts.size() > 120)
            self->m_decodeStarts.erase(self->m_decodeStarts.begin());
    } else {
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
    {
        QMutexLocker lock(&m_timingMutex);
        timings = m_decodeTimes;
    }
    std::sort(timings.begin(), timings.end());
    return {{"camera", m_name},
            {"decode_samples", timings.size()},
            {"decode_median_ms", timings.isEmpty() ? -1.0 : timings[timings.size() / 2]},
            {"decode_p95_ms", timings.isEmpty() ? -1.0 : timings[qMin(timings.size() - 1, timings.size() * 95 / 100)]},
            {"rendered", qulonglong(m_rendered)},
            {"dropped", qulonglong(m_dropped)},
            {"sink_fps", m_renderedFps},
            {"status", m_status},
            {"media", m_media.toString()},
            {"sample_ms", m_lastPoll},
            {"position", m_position},
            {"paused", m_paused},
            {"generation", m_generation.load()}};
}
