#include "model.h"
#include <QBuffer>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSet>
#include <QVector>
#include <QtEndian>
#include <algorithm>
#include <cstring>
#include <cmath>
#include <limits>

static quint32 word(const QByteArray &data, int offset) {
    return qFromLittleEndian<quint32>(reinterpret_cast<const uchar *>(data.constData() + offset));
}
static void appendWord(QByteArray &data, quint32 value) {
    char bytes[4];
    qToLittleEndian(value, reinterpret_cast<uchar *>(bytes));
    data.append(bytes, 4);
}
static bool integer(const QJsonObject &object, const char *key, qint64 &value, qint64 fallback = -1) {
    const auto field = object.value(QLatin1String(key));
    if (field.isUndefined()) { value = fallback; return fallback >= 0; }
    const double number = field.toDouble(-1);
    if (!field.isDouble() || !std::isfinite(number) || number < 0 ||
        number > std::numeric_limits<int>::max() || std::floor(number) != number) return false;
    value = qint64(number);
    return true;
}
QByteArray compatibleGlb(const QByteArray &source, QString &error) {
    error.clear();
    constexpr qint64 limit = 256 * 1024 * 1024;
    if (source.size() > limit) {
        error = "GLB exceeds the model conversion size limit";
        return {};
    }
    if (source.size() < 28 || source.left(4) != "glTF" || word(source, 4) != 2) {
        error = "Invalid GLB header";
        return {};
    }
    if (word(source, 8) != quint32(source.size()) || source.mid(16, 4) != "JSON") {
        error = "Invalid GLB chunks";
        return {};
    }
    const quint32 jsonSize = word(source, 12);
    if (jsonSize > quint32(source.size() - 28)) {
        error = "Invalid GLB JSON size";
        return {};
    }
    const auto parsed = QJsonDocument::fromJson(source.mid(20, jsonSize));
    if (!parsed.isObject()) { error = "Invalid GLB document"; return {}; }
    auto document = parsed.object();
    auto extensions = document["extensionsUsed"].toArray();
    if (!extensions.contains("KHR_mesh_quantization") && !extensions.contains("EXT_texture_webp"))
        return source;
    const int binaryOffset = 28 + jsonSize;
    if (source.mid(24 + jsonSize, 4) != QByteArray("BIN\0", 4) ||
        word(source, 20 + jsonSize) > quint32(source.size() - binaryOffset)) {
        error = "Invalid GLB binary chunk";
        return {};
    }
    QByteArray binary = source.mid(binaryOffset, word(source, 20 + jsonSize));
    const auto buffers = document["buffers"].toArray();
    qint64 bufferLength;
    if (buffers.size() != 1 || !buffers[0].isObject() || buffers[0].toObject().contains("uri") ||
        !integer(buffers[0].toObject(), "byteLength", bufferLength) || bufferLength > binary.size()) {
        error = "Unsupported GLB buffer layout";
        return {};
    }
    auto views = document["bufferViews"].toArray(), accessors = document["accessors"].toArray();
    auto validView = [&](int index) {
        if (index < 0 || index >= views.size() || !views[index].isObject()) return false;
        const auto view = views[index].toObject();
        qint64 buffer, offset, length;
        return integer(view, "buffer", buffer) && buffer == 0 &&
            integer(view, "byteOffset", offset, 0) && integer(view, "byteLength", length) &&
            length > 0 && offset <= bufferLength && length <= bufferLength - offset;
    };
    auto appendView = [&](const QByteArray &bytes) -> int {
        if (bytes.size() > limit - binary.size() - 3) return -1;
        while (binary.size() % 4)
            binary.append('\0');
        int offset = binary.size();
        binary.append(bytes);
        views.append(QJsonObject{{"buffer", 0}, {"byteOffset", offset}, {"byteLength", bytes.size()}});
        return views.size() - 1;
    };
    QSet<int> attributes;
    for (auto mesh : document["meshes"].toArray())
        for (auto primitive : mesh.toObject()["primitives"].toArray()) {
            auto a = primitive.toObject()["attributes"].toObject();
            for (auto it = a.begin(); it != a.end(); ++it)
                if (!it.key().startsWith("JOINTS_")) {
                    qint64 index;
                    if (!integer(a, it.key().toUtf8().constData(), index) || index >= accessors.size()) {
                        error = "Invalid attribute accessor index";
                        return {};
                    }
                    attributes.insert(int(index));
                }
        }
    for (int index : attributes) {
        if (!accessors[index].isObject()) { error = "Invalid accessor"; return {}; }
        auto a = accessors[index].toObject();
        qint64 componentType;
        if (!integer(a, "componentType", componentType)) { error = "Invalid component type"; return {}; }
        int type = int(componentType);
        if (type == 5126)
            continue;
        const int width = type == 5120 || type == 5121 ? 1 : type == 5122 || type == 5123 ? 2 : 0;
        const QString shape = a["type"].toString();
        int components = shape == "VEC2" ? 2 : shape == "VEC3" ? 3 : shape == "VEC4" ? 4 : 0;
        if (!width || !components || a.contains("sparse")) {
            error = "Unsupported quantized accessor";
            return {};
        }
        qint64 viewIndex, count, accessorOffset;
        if (!integer(a, "bufferView", viewIndex) || !validView(int(viewIndex)) ||
            !integer(a, "count", count) || count <= 0 || !integer(a, "byteOffset", accessorOffset, 0)) {
            error = "Invalid quantized accessor range";
            return {};
        }
        const auto view = views[int(viewIndex)].toObject();
        qint64 viewOffset, viewLength, stride;
        integer(view, "byteOffset", viewOffset, 0);
        integer(view, "byteLength", viewLength);
        const qint64 elementSize = width * components;
        if (!integer(view, "byteStride", stride, elementSize) || stride < elementSize || stride % width ||
            (viewOffset + accessorOffset) % width || accessorOffset > viewLength ||
            elementSize > viewLength - accessorOffset ||
            count - 1 > (viewLength - accessorOffset - elementSize) / stride ||
            count > limit / (components * 4)) {
            error = "Invalid quantized buffer range";
            return {};
        }
        const qint64 offset = viewOffset + accessorOffset;
        QByteArray expanded;
        expanded.resize(int(count * components * 4));
        QVector<float> lo(components, 1e30f), hi(components, -1e30f);
        for (int i = 0; i < count; ++i)
            for (int c = 0; c < components; ++c) {
                const auto *p =
                    reinterpret_cast<const uchar *>(binary.constData() + offset + qint64(i) * stride + c * width);
                float value = type == 5120   ? qint8(*p)
                              : type == 5121 ? *p
                              : type == 5122 ? qFromLittleEndian<qint16>(p)
                                             : qFromLittleEndian<quint16>(p);
                if (a["normalized"].toBool()) {
                    float divisor = type == 5120 ? 127 : type == 5121 ? 255 : type == 5122 ? 32767 : 65535;
                    value = std::max(type == 5120 || type == 5122 ? -1.f : 0.f, value / divisor);
                }
                std::memcpy(expanded.data() + (i * components + c) * 4, &value, 4);
                lo[c] = std::min(lo[c], value);
                hi[c] = std::max(hi[c], value);
            }
        const int expandedView = appendView(expanded);
        if (expandedView < 0) { error = "Converted GLB exceeds size limit"; return {}; }
        a["bufferView"] = expandedView;
        a["byteOffset"] = 0;
        a["componentType"] = 5126;
        a.remove("normalized");
        QJsonArray minimum, maximum;
        for (int c = 0; c < components; ++c) {
            minimum.append(lo[c]);
            maximum.append(hi[c]);
        }
        a["min"] = minimum;
        a["max"] = maximum;
        accessors[index] = a;
    }
    auto images = document["images"].toArray(), textures = document["textures"].toArray();
    for (int i = 0; i < textures.size(); ++i) {
        auto t = textures[i].toObject(), ext = t["extensions"].toObject();
        if (!ext.contains("EXT_texture_webp"))
            continue;
        qint64 imageIndex, viewIndex;
        if (!integer(ext["EXT_texture_webp"].toObject(), "source", imageIndex) ||
            imageIndex >= images.size() || !images[int(imageIndex)].isObject()) {
            error = "Invalid WebP image index";
            return {};
        }
        const int index = int(imageIndex);
        auto im = images[index].toObject();
        if (!integer(im, "bufferView", viewIndex) || !validView(int(viewIndex))) {
            error = "Invalid WebP image view";
            return {};
        }
        const auto v = views[int(viewIndex)].toObject();
        const auto bytes = binary.mid(v["byteOffset"].toInt(), v["byteLength"].toInt());
        auto decoded = QImage::fromData(bytes);
        QByteArray png;
        QBuffer buffer(&png);
        buffer.open(QIODevice::WriteOnly);
        if (decoded.isNull() || !decoded.save(&buffer, "PNG")) {
            error = "Qt WebP image plugin is required";
            return {};
        }
        const int imageView = appendView(png);
        if (imageView < 0) { error = "Converted GLB exceeds size limit"; return {}; }
        im["bufferView"] = imageView;
        im["mimeType"] = "image/png";
        images[index] = im;
        t["source"] = index;
        ext.remove("EXT_texture_webp");
        t["extensions"] = ext;
        textures[i] = t;
    }
    document["bufferViews"] = views;
    document["accessors"] = accessors;
    document["images"] = images;
    document["textures"] = textures;
    document["buffers"] = QJsonArray{QJsonObject{{"byteLength", binary.size()}}};
    for (const auto *key : {"extensionsUsed", "extensionsRequired"}) {
        QJsonArray keep;
        for (auto v : document[key].toArray())
            if (v != "KHR_mesh_quantization" && v != "EXT_texture_webp")
                keep.append(v);
        document[key] = keep;
    }
    auto json = QJsonDocument(document).toJson(QJsonDocument::Compact);
    while (json.size() % 4)
        json.append(' ');
    while (binary.size() % 4)
        binary.append('\0');
    if (28 + qint64(json.size()) + binary.size() > limit) {
        error = "Converted GLB exceeds size limit";
        return {};
    }
    QByteArray out("glTF");
    appendWord(out, 2);
    appendWord(out, 28 + json.size() + binary.size());
    appendWord(out, json.size());
    out.append("JSON");
    out.append(json);
    appendWord(out, binary.size());
    out.append("BIN\0", 4);
    out.append(binary);
    return out;
}
