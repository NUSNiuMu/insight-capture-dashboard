#pragma once

#include <QQuaternion>
#include <QVector3D>
#include <QtGlobal>
#include <cmath>

class DisplayPoseFilter {
  public:
    QVector3D position;
    QQuaternion rotation;

    void reset() { m_ready = false; }

    void update(const QVector3D &inputPosition, const QQuaternion &inputRotation,
                qint64 nowMs, bool bypass = false) {
        const auto orientation = inputRotation.normalized();
        const double dt = (nowMs - m_lastMs) / 1000.0;
        // Display coordinates are centimeters. Discontinuities and replay
        // must snap rather than interpolate across unrelated timeline samples.
        if (bypass || !m_ready || dt <= 0 || dt > .25 ||
            (inputPosition - position).length() > 2.0f ||
            angle(rotation, orientation) > 5.0) {
            position = inputPosition;
            rotation = orientation;
            m_speed = m_angularSpeed = 0;
        } else {
            const double derivativeAlpha = alpha(1.0, dt);
            m_speed += derivativeAlpha * (
                (inputPosition - m_previousPosition).length() / dt - m_speed);
            m_angularSpeed += derivativeAlpha * (
                angle(m_previousRotation, orientation) / dt - m_angularSpeed);
            // Low motion attenuates sensor noise; real motion raises the
            // cutoff without adding a frame queue or a rendering timer.
            const double positionAlpha = alpha(qMin(30.0, 3.0 + .6 * m_speed), dt);
            const double rotationAlpha = alpha(qMin(30.0, 3.0 + .4 * m_angularSpeed), dt);
            position += float(positionAlpha) * (inputPosition - position);
            rotation = QQuaternion::slerp(rotation, orientation, float(rotationAlpha)).normalized();
        }
        m_previousPosition = inputPosition;
        m_previousRotation = orientation;
        m_lastMs = nowMs;
        m_ready = true;
    }

  private:
    static double alpha(double cutoff, double dt) {
        return 1.0 - std::exp(-6.283185307179586 * cutoff * dt);
    }
    static double angle(const QQuaternion &left, const QQuaternion &right) {
        const double dotProduct = double(left.scalar()) * right.scalar() +
            double(left.x()) * right.x() + double(left.y()) * right.y() + double(left.z()) * right.z();
        const auto norm = [](const QQuaternion &q) {
            return std::sqrt(double(q.scalar()) * q.scalar() + double(q.x()) * q.x() +
                double(q.y()) * q.y() + double(q.z()) * q.z());
        };
        const double dot = std::abs(dotProduct) / (norm(left) * norm(right));
        return 114.59155902616465 * std::acos(qBound(0.0, dot, 1.0));
    }
    bool m_ready = false;
    qint64 m_lastMs = 0;
    double m_speed = 0, m_angularSpeed = 0;
    QVector3D m_previousPosition;
    QQuaternion m_previousRotation;
};
