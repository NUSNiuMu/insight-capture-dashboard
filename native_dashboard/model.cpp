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

static quint32 word(const QByteArray &data, int offset) {
    return qFromLittleEndian<quint32>(reinterpret_cast<const uchar *>(data.constData() + offset));
}
static void appendWord(QByteArray &data, quint32 value) {
    char bytes[4];
    qToLittleEndian(value, reinterpret_cast<uchar *>(bytes));
    data.append(bytes, 4);
}
QByteArray compatibleGlb(const QByteArray &source, QString &error) {
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
    auto document = QJsonDocument::fromJson(source.mid(20, jsonSize)).object();
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
    auto views = document["bufferViews"].toArray(), accessors = document["accessors"].toArray();
    auto appendView = [&](const QByteArray &bytes) {
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
                if (!it.key().startsWith("JOINTS_"))
                    attributes.insert(it.value().toInt());
        }
    for (int index : attributes) {
        auto a = accessors[index].toObject();
        int type = a["componentType"].toInt();
        if (type == 5126)
            continue;
        const int width = type == 5120 || type == 5121 ? 1 : type == 5122 || type == 5123 ? 2 : 0;
        const QString shape = a["type"].toString();
        int components = shape == "VEC2" ? 2 : shape == "VEC3" ? 3 : shape == "VEC4" ? 4 : 0;
        if (!width || !components || a.contains("sparse")) {
            error = "Unsupported quantized accessor";
            return {};
        }
        auto view = views[a["bufferView"].toInt()].toObject();
        int count = a["count"].toInt(), offset = view["byteOffset"].toInt() + a["byteOffset"].toInt(),
            stride = view["byteStride"].toInt(width * components);
        if (count <= 0 || offset < 0 ||
            qint64(offset) + qint64(count - 1) * stride + width * components > binary.size()) {
            error = "Invalid quantized buffer range";
            return {};
        }
        QByteArray expanded;
        expanded.resize(count * components * 4);
        QVector<float> lo(components, 1e30f), hi(components, -1e30f);
        for (int i = 0; i < count; ++i)
            for (int c = 0; c < components; ++c) {
                const auto *p =
                    reinterpret_cast<const uchar *>(binary.constData() + offset + i * stride + c * width);
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
        a["bufferView"] = appendView(expanded);
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
        int index = ext["EXT_texture_webp"].toObject()["source"].toInt();
        auto im = images[index].toObject(), v = views[im["bufferView"].toInt()].toObject();
        auto bytes = binary.mid(v["byteOffset"].toInt(), v["byteLength"].toInt());
        auto decoded = QImage::fromData(bytes);
        QByteArray png;
        QBuffer buffer(&png);
        buffer.open(QIODevice::WriteOnly);
        if (decoded.isNull() || !decoded.save(&buffer, "PNG")) {
            error = "Qt WebP image plugin is required";
            return {};
        }
        im["bufferView"] = appendView(png);
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
