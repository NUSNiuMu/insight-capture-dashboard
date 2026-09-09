#include "geometry.h"
#include <QByteArray>
#include <algorithm>

TrailGeometry::TrailGeometry(QQuick3DObject *parent) : QQuick3DGeometry(parent) { setPoints({}); }
void TrailGeometry::setPoints(const QVector<QVector3D> &points) {
    clear();
    setStride(3 * sizeof(float));
    setPrimitiveType(QQuick3DGeometry::PrimitiveType::Lines);
    addAttribute(QQuick3DGeometry::Attribute::PositionSemantic, 0, QQuick3DGeometry::Attribute::F32Type);
    QByteArray data;
    data.resize(std::max(1, int(points.size()) - 1) * 6 * sizeof(float));
    if (points.size() < 2)
        data.fill(0);
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
        for (const auto &p : {points[i - 1], v}) {
            *out++ = p.x();
            *out++ = p.y();
            *out++ = p.z();
        }
    }
    setVertexData(data);
    setBounds(points.isEmpty() ? QVector3D() : lo, points.isEmpty() ? QVector3D() : hi);
    update();
}
