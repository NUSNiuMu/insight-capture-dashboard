#include "geometry.h"
#include <QByteArray>
#include <algorithm>

TrailGeometry::TrailGeometry(QQuick3DObject *parent) : QQuick3DGeometry(parent) { setPoints({}); }
void TrailGeometry::setPoints(const QVector<QVector3D> &points) {
    clear();
    setStride(8 * sizeof(float));
    setPrimitiveType(QQuick3DGeometry::PrimitiveType::Triangles);
    addAttribute(QQuick3DGeometry::Attribute::PositionSemantic, 0, QQuick3DGeometry::Attribute::F32Type);
    // Segment direction and endpoint/side let the shader expand in screen pixels.
    addAttribute(QQuick3DGeometry::Attribute::NormalSemantic, 3 * sizeof(float),
                 QQuick3DGeometry::Attribute::F32Type);
    addAttribute(QQuick3DGeometry::Attribute::TexCoord0Semantic, 6 * sizeof(float),
                 QQuick3DGeometry::Attribute::F32Type);
    QByteArray data;
    data.resize(std::max(1, int(points.size()) - 1) * 6 * 8 * sizeof(float));
    auto *out = reinterpret_cast<float *>(data.data());
    QVector3D lo(1e8, 1e8, 1e8), hi(-1e8, -1e8, -1e8);
    for (int i = 0; i < points.size(); ++i) {
        const auto &v = points[i];
        for (int axis = 0; axis < 3; ++axis) {
            lo[axis] = std::min(lo[axis], v[axis]);
            hi[axis] = std::max(hi[axis], v[axis]);
        }
        if (!i)
            continue;
        const auto direction = v - points[i - 1];
        if (direction.lengthSquared() < 1e-10f)
            continue;
        const float corners[6][2] = {{0, -1}, {1, -1}, {1, 1}, {0, -1}, {1, 1}, {0, 1}};
        for (const auto &corner : corners) {
            const auto &p = corner[0] == 0 ? points[i - 1] : v;
            *out++ = p.x();
            *out++ = p.y();
            *out++ = p.z();
            *out++ = direction.x();
            *out++ = direction.y();
            *out++ = direction.z();
            *out++ = corner[0];
            *out++ = corner[1];
        }
    }
    const auto used = reinterpret_cast<char *>(out) - data.constData();
    // Qt can rebuild a bound geometry before the model's visibility update arrives.
    data.resize(used ? used : 6 * 8 * sizeof(float));
    if (!used)
        data.fill(0);
    setVertexData(data);
    setBounds(points.isEmpty() ? QVector3D() : lo, points.isEmpty() ? QVector3D() : hi);
    update();
}
