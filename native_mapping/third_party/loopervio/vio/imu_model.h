// Copyright 2021 DeepMirror Inc. All rights reserved.

#ifndef VIO_IMU_MODEL_H_
#define VIO_IMU_MODEL_H_

#include "Eigen/Dense"
#include "glog/logging.h"

namespace dm {
namespace vio {

// Single source of truth for how a raw IMU sample relates to the true angular rate and
// specific force. Every place that turns (measurement, bias) into a physical quantity must
// go through this struct, so the model is stated once instead of being re-derived in the
// preintegration, the filter propagation and the pose predictor independently.
//
// Sensor model (Kalibr "scale-misalignment", Rehder2017; identical to OpenVINS' KALIBR
// model, see open_vins/docs/propagation-analytical.dox):
//
//     a_m = Ma * a_true + b_a + n_a
//     w_m = Mw * C_gyro_i * w_true + Tg * a_true + b_g + n_g
//
// with Ma, Mw lower-triangular (scale on the diagonal, axis misalignment below it),
// C_gyro_i the rotation from the accelerometer frame -- taken as the IMU body frame -- to
// the gyroscope frame, and Tg the gyroscope's sensitivity to specific force.
//
// Solving for the true quantities gives what this struct actually applies:
//
//     a = Da  * (a_m - b_a)               Da  = Ma^-1
//     w = RDw * (w_m - b_g - Tg * a)      RDw = C_gyro_i^T * Mw^-1
//
// The inverses are stored rather than Ma/Mw so the hot path needs no runtime inversion;
// SensorCalibration performs the inversion once at load time. Note the gyro correction
// consumes the ALREADY-CORRECTED specific force, so callers must feed it the output of
// Accel(), never the raw measurement.
//
// Defaults are identity/zero, which reduce exactly to the previous model (a_m - b_a,
// w_m - b_g): a calibration file carrying no IMU intrinsics behaves as before.
struct ImuModel {
  Eigen::Matrix3d Da = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d RDw = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d Tg = Eigen::Matrix3d::Zero();
  // Forward, measurement-domain accelerometer matrix (Kalibr's Ma; the inverse of Da). Only
  // the cold paths that go the other way need it: the static initialiser knows the true
  // specific force (it is -gravity) and solves for the bias that produced the measurement,
  // which requires predicting a measurement rather than correcting one. Nothing on the hot
  // path uses it, so the "store the inverses" rule above still holds where it matters.
  Eigen::Matrix3d Ma = Eigen::Matrix3d::Identity();

  // Corrected specific force in the IMU (== accelerometer) frame.
  Eigen::Vector3d Accel(const Eigen::Vector3d& accel_meas,
                        const Eigen::Vector3d& accel_bias) const {
    return Da * (accel_meas - accel_bias);
  }

  // Corrected angular rate in the IMU frame. `accel` MUST be the output of Accel() for the
  // same instant; passing the raw measurement silently mis-applies the g-sensitivity term.
  Eigen::Vector3d Gyro(const Eigen::Vector3d& gyro_meas, const Eigen::Vector3d& gyro_bias,
                       const Eigen::Vector3d& accel) const {
    return RDw * (gyro_meas - gyro_bias - Tg * accel);
  }

  // Convenience for the common "correct both at once" case.
  void Correct(const Eigen::Vector3d& accel_meas, const Eigen::Vector3d& gyro_meas,
               const Eigen::Vector3d& accel_bias, const Eigen::Vector3d& gyro_bias,
               Eigen::Vector3d* accel_out, Eigen::Vector3d* gyro_out) const {
    *accel_out = Accel(accel_meas, accel_bias);
    *gyro_out = Gyro(gyro_meas, gyro_bias, *accel_out);
  }

  // Partials of the corrected quantities w.r.t. the bias states, collected here so the
  // transition matrices in the preintegration and the filter do not each re-derive them.
  // With the defaults these are -I, -I and 0, i.e. the classic model.
  Eigen::Matrix3d dAccel_dAccelBias() const { return -Da; }
  Eigen::Matrix3d dGyro_dGyroBias() const { return -RDw; }
  // Nonzero only with g-sensitivity: the gyro correction subtracts Tg * Accel(), and
  // Accel() itself depends on the accelerometer bias.
  Eigen::Matrix3d dGyro_dAccelBias() const { return RDw * Tg * Da; }

  bool IsIdentity() const {
    return Da.isIdentity() && RDw.isIdentity() && Tg.isZero() && Ma.isIdentity();
  }

  // Build from the matrices exactly as Kalibr reports them under --imu-models
  // scale-misalignment, i.e. `Ma`/`Mw` are its "M" blocks, `Tg` its "A" block and `C_gyro_i`
  // its "C_gyro_i". This is the only place the model is inverted; storing Kalibr's own
  // matrices in the calibration file (rather than the inverses) keeps the file directly
  // comparable to the calibration report.
  //
  // Ma and Mw are lower triangular with the scale factors on the diagonal, so they are
  // invertible for any sane calibration; a singular one is a corrupt calibration and the
  // caller gets the identity model back rather than a matrix full of infinities.
  static ImuModel FromKalibr(const Eigen::Matrix3d& Ma, const Eigen::Matrix3d& Mw,
                             const Eigen::Matrix3d& C_gyro_i, const Eigen::Matrix3d& Tg) {
    ImuModel m;
    const Eigen::FullPivLU<Eigen::Matrix3d> lu_a(Ma), lu_w(Mw);
    if (!lu_a.isInvertible() || !lu_w.isInvertible()) {
      LOG(ERROR) << "Singular IMU intrinsics (Ma or Mw); falling back to the identity model.";
      return ImuModel();
    }
    m.Da = lu_a.inverse();
    m.Ma = Ma;
    m.RDw = C_gyro_i.transpose() * lu_w.inverse();
    m.Tg = Tg;
    return m;
  }
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_IMU_MODEL_H_
