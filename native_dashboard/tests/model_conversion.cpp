#include "model.h"
#include <QCoreApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QtEndian>
#include <cmath>
#include <cstring>

static void appendWord(QByteArray &bytes, quint32 value) {
    char data[4];
    qToLittleEndian(value, reinterpret_cast<uchar *>(data));
    bytes.append(data, 4);
}
static QByteArray glb(QJsonObject document, QByteArray binary) {
    auto json = QJsonDocument(document).toJson(QJsonDocument::Compact);
    while (json.size() % 4) json.append(' ');
    while (binary.size() % 4) binary.append('\0');
    QByteArray result("glTF");
    for (auto value : {quint32(2), quint32(28 + json.size() + binary.size()), quint32(json.size())})
        appendWord(result, value);
    result.append("JSON"); result.append(json);
    appendWord(result, binary.size()); result.append("BIN\0", 4); result.append(binary);
    return result;
}
int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    // Two ordinary normalized byte positions with a four-byte vertex stride.
    const QByteArray binary = QByteArray::fromHex("007fff00ff004000");
    QJsonObject document{{"asset", QJsonObject{{"version", "2.0"}}},
        {"extensionsUsed", QJsonArray{"KHR_mesh_quantization"}},
        {"buffers", QJsonArray{QJsonObject{{"byteLength", 8}}}},
        {"bufferViews", QJsonArray{QJsonObject{{"buffer", 0}, {"byteOffset", 0}, {"byteLength", 8}, {"byteStride", 4}}}},
        {"accessors", QJsonArray{QJsonObject{{"bufferView", 0}, {"componentType", 5121}, {"normalized", true}, {"count", 2}, {"type", "VEC3"}}}},
        {"meshes", QJsonArray{QJsonObject{{"primitives", QJsonArray{QJsonObject{{"attributes", QJsonObject{{"POSITION", 0}}}}}}}}}};
    QString error;
    const auto output = compatibleGlb(glb(document, binary), error);
    if (output.isEmpty() || !error.isEmpty()) return 1;
    const auto size = qFromLittleEndian<quint32>(reinterpret_cast<const uchar *>(output.constData() + 12));
    const auto converted = QJsonDocument::fromJson(output.mid(20, size)).object();
    const auto accessor = converted["accessors"].toArray()[0].toObject();
    if (accessor["componentType"].toInt() != 5126 || accessor["normalized"].toBool()) return 2;
    const auto view = converted["bufferViews"].toArray()[accessor["bufferView"].toInt()].toObject();
    if (view["byteLength"].toInt() != 24) return 3;
    const float expected[] = {0, 127.f/255, 1, 1, 0, 64.f/255};
    for (int i=0; i<6; ++i) {
        float value;
        std::memcpy(&value, output.constData() + 28 + size + view["byteOffset"].toInt() + 4*i, 4);
        if (std::abs(value - expected[i]) > .00001f) return 4;
    }
    // Core glTF needs no extension conversion.
    document.remove("extensionsUsed");
    const auto original = glb(document, binary);
    if (compatibleGlb(original, error) != original || !error.isEmpty()) return 5;
    return 0;
}
