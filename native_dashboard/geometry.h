#pragma once
#include <QQuick3DGeometry>
#include <QVector3D>

class TrailGeometry : public QQuick3DGeometry {
    Q_OBJECT
  public:
    explicit TrailGeometry(QQuick3DObject *parent = nullptr);
    void setPoints(const QVector<QVector3D> &points);
};
