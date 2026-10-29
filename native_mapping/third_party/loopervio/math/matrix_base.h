// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef MATH_MATRIX_BASE_H_
#define MATH_MATRIX_BASE_H_

#include <functional>
#include <initializer_list>
#include <memory>
#include <unordered_map>
#include <utility>
#include <vector>
#include <fstream>
#include <iostream>
#include <string>
#include <algorithm>
#include "glog/logging.h"
#include "Eigen/Eigen"

namespace Eigen {
typedef Eigen::Matrix<float, 3, 4> Matrix3x4f;
typedef Eigen::Matrix<double, 3, 4> Matrix3x4d;
typedef Eigen::Matrix<double, 6, 6> Matrix6d;
typedef Eigen::Matrix<uint8_t, 3, 1> Vector3ub;
typedef Eigen::Matrix<uint8_t, 4, 1> Vector4ub;

using Vector6d = Eigen::Matrix<double, 6, 1>;
using Vector6f = Eigen::Matrix<float, 6, 1>;

// using RatrixXd = MatrixXd;
using RatrixXd = Eigen::Matrix<double, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor>;
using RatrixXf = Eigen::Matrix<float, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor>;

using Ratrix2d = Eigen::Matrix<double, 2, 2, Eigen::RowMajor>;
using Ratrix3d = Eigen::Matrix<double, 3, 3, Eigen::RowMajor>;
using Ratrix4d = Eigen::Matrix<double, 4, 4, Eigen::RowMajor>;

using Ratrix2f = Eigen::Matrix<float, 2, 2, Eigen::RowMajor>;
using Ratrix3f = Eigen::Matrix<float, 3, 3, Eigen::RowMajor>;
using Ratrix4f = Eigen::Matrix<float, 4, 4, Eigen::RowMajor>;

template<typename T>
int GetScalarType() {
  if constexpr (std::is_same<T, int>::value) {
    return 0;
  } else if constexpr (std::is_same<T, double>::value) {
    return 1;
  } else if constexpr (std::is_same<T, float>::value) {
    return 2;
  } else {
    return -1;
  }
}

template<typename MatrixType>
void WriteMatrix(const std::string& filename, const MatrixType& matrix) {
  std::ofstream file(filename, std::ios::binary);
  if (file.is_open()) {
    int rows = matrix.rows();
    int cols = matrix.cols();
    int is_row_major = matrix.IsRowMajor;
    int type = GetScalarType<typename MatrixType::Scalar>();
    file.write(reinterpret_cast<const char*>(&rows), sizeof(int));
    file.write(reinterpret_cast<const char*>(&cols), sizeof(int));
    file.write(reinterpret_cast<const char*>(&is_row_major), sizeof(int));
    file.write(reinterpret_cast<const char*>(&type), sizeof(int));
    file.write(reinterpret_cast<const char*>(matrix.data()),
               rows * cols * sizeof(typename MatrixType::Scalar));
    file.close();
  } else {
    std::cerr << "Error opening file for writing: " << filename << std::endl;
  }
}

template<typename MatrixType>
MatrixType ReadMatrix(const std::string& filename) {
  MatrixType matrix;
  std::ifstream file(filename, std::ios::binary);
  if (file.is_open()) {
    int rows, cols, is_row_major, type;
    file.read(reinterpret_cast<char*>(&rows), sizeof(int));
    file.read(reinterpret_cast<char*>(&cols), sizeof(int));
    file.read(reinterpret_cast<char*>(&is_row_major), sizeof(int));
    file.read(reinterpret_cast<char*>(&type), sizeof(int));
    assert(is_row_major == matrix.IsRowMajor);
    assert(type == GetScalarType<typename MatrixType::Scalar>());
    matrix.resize(rows, cols);
    file.read(reinterpret_cast<char*>(matrix.data()),
              rows * cols * sizeof(typename MatrixType::Scalar));
    file.close();
  } else {
    std::cerr << "Error opening file for reading: " << filename << std::endl;
  }
  return matrix;
}

}  // namespace Eigen

#ifndef EIGEN_ALIGNED_ALLOCATOR
#define EIGEN_ALIGNED_ALLOCATOR Eigen::aligned_allocator
#endif

namespace dm {
namespace vio {
template<typename Matrix>
void ConservativeResize(Matrix* mat, int new_rows, int new_cols) {
  int old_rows = mat->rows();
  int old_cols = mat->cols();
  mat->conservativeResize(new_rows, new_cols);
  mat->block(0, old_cols, new_rows, new_cols - old_cols).setZero();
  mat->block(old_rows, 0, new_rows - old_rows, new_cols).setZero();
  return;
}

template<typename Matrix, typename Vector>
void QRCompressInplace(Matrix* A, Vector* b, int start_col = 0, int end_col = -1) {
  if (A->rows() < A->cols()) return;
  if (end_col < 0) end_col = A->cols();
  Eigen::JacobiRotation<typename Matrix::Scalar> tempHo_GR;
  typename Matrix::Scalar x = 0, y = 0;
  for (int n = start_col; n < end_col; n++) {
    for (int m = A->rows() - 1; m > n; --m) {
      y = (*A)(m, n);
      if (fabs(y) < 1e-8) {
        continue;
      }
      x = (*A)(m - 1, n);
      tempHo_GR.makeGivens(x, y);
      const auto rot = tempHo_GR.adjoint();
      A->block(m - 1, n, 2, A->cols() - n).applyOnTheLeft(0, 1, rot);
      b->segment(m - 1, 2).applyOnTheLeft(0, 1, rot);
    }
  }
  if (end_col != A->cols()) return;
  int r = A->cols();
  (*A).conservativeResize(r, r);
  A->template triangularView<Eigen::StrictlyLower>().setZero();
  (*b).conservativeResize(r, 1);
}

template<typename Matrix, typename Vector>
void QRRCompressInplace(Matrix* A, Vector* b, int start_col = 0, int end_col = -1) {
  if (A->rows() < A->cols()) return;
  if (end_col < 0) end_col = A->cols();
  Eigen::JacobiRotation<typename Matrix::Scalar> tempHo_GR;
  typename Matrix::Scalar x = 0, y = 0;
  for (size_t n = start_col; n < end_col; n++) {
    for (size_t m = A->rows() - 1; m > n; --m) {
      x = (*A)(m - 1, n);
      y = (*A)(m, n);
      if (fabs(y) < 1e-8) {
        continue;
      }
      typename Matrix::Scalar v = sqrt(x * x + y * y);
      typename Matrix::Scalar c = x / v;
      typename Matrix::Scalar s = y / v;
      for (size_t k = 0; k < A->cols() - n; k++) {
        typename Matrix::Scalar xi = (*A)(m-1, n+k);
        typename Matrix::Scalar yi = (*A)(m, n+k);
        (*A)(m-1, n+k) = c * xi + s * yi;
        (*A)(m, n+k) = -s * xi + c * yi;
      }
      typename Matrix::Scalar b0 = (*b)[m - 1];
      typename Matrix::Scalar b1 = (*b)[m];
      (*b)[m - 1] = c * b0 + s * b1;
      (*b)[m] = -s * b0 + c * b1;
    }
  }
  if (end_col != A->cols()) return;
  int r = A->cols();
  (*A).conservativeResize(r, r);
  A->template triangularView<Eigen::StrictlyLower>().setZero();
  (*b).conservativeResize(r, 1);
}

template<typename Matrix, typename Vector>
void NullspaceProjectInplace(Matrix* H_f, Matrix* H_x, Vector* res, bool crop = true) {
  Eigen::JacobiRotation<typename Matrix::Scalar> tempHo_GR;
  for (size_t n = 0, nend = H_f->cols(); n < nend; ++n) {
    for (size_t m = H_f->rows() - 1; m > n; --m) {
      // Givens matrix G
      tempHo_GR.makeGivens((*H_f)(m - 1, n), (*H_f)(m, n));
      H_f->block(m - 1, n, 2, H_f->cols() - n).applyOnTheLeft(0, 1, tempHo_GR.adjoint());
      H_x->middleRows(m - 1, 2).applyOnTheLeft(0, 1, tempHo_GR.adjoint());
      res->segment(m - 1, 2).applyOnTheLeft(0, 1, tempHo_GR.adjoint());
    }
  }
  (*H_f) = H_f->topRows(H_f->cols()).eval();
  H_f->template triangularView<Eigen::StrictlyLower>().setZero();
  if (crop) {
    (*H_x) = H_x->block(H_f->cols(), 0, H_x->rows() - H_f->cols(), H_x->cols()).eval();
    (*res) = res->block(H_f->cols(), 0, res->rows() - H_f->cols(), res->cols()).eval();
  }
}

template<typename Matrix, typename Vector, typename G = float>
class QRRCompress {
 public:
  QRRCompress(size_t r0, size_t c0, std::vector<int> z_indices)
      : r0_(r0), c0_(c0), z_indices_(z_indices) {}

  void operator()(Matrix* A, Vector* b) { return QRRCompressInplace(A, b); }

  void QRRCompressInplace(Matrix* A, Vector* b) {
    if (A->rows() < A->cols()) return;
    Eigen::JacobiRotation<G> tempHo_GR;
    G x = 0, y = 0;
    for (int n = 0; n < c0_; n++) {
      for (int m = A->rows() - 1; m > n; --m) {
        y = (*A)(m, n);
        if (fabs(y) < 1e-8) {
          continue;
        }
        x = (*A)(m - 1, n);
        tempHo_GR.makeGivens(x, y);
        const auto rot = tempHo_GR.adjoint();
        if (m - 1 < r0_) {
          A->block(m - 1, n, 2, A->cols() - n).applyOnTheLeft(0, 1, rot);
        } else {
          A->block(m - 1, n, 2, c0_ - n).applyOnTheLeft(0, 1, rot);
          if (z_indices_[m - 1] != -1) {
            int s = z_indices_[m - 1];
            A->block(m - 1, s, 2, A->cols() - s).applyOnTheLeft(0, 1, rot);
          }
        }
        z_indices_[m] = z_indices_[m - 1];
        b->segment(m - 1, 2).applyOnTheLeft(0, 1, rot);
      }
    }
  }

 private:
  size_t r0_;
  size_t c0_;
  std::vector<int> z_indices_;
};

template<typename Ratrix>
Ratrix TriangularSquare(const Ratrix& mat) {
  Eigen::MatrixXd result = Ratrix::Zero(mat.rows(), mat.cols());
  size_t length = mat.rows();
  for (size_t r = 0; r < result.rows(); r++) {
    for (size_t c = r; c < result.cols(); c++) {
      size_t l = length - c;
      const auto* ptr0 = &mat(r, c);
      const auto* ptr1 = &mat(c, c);
      for (size_t i = 0; i < l; i++) {
        result(r, c) += *ptr0 * *ptr1;
        ptr0++;
        ptr1++;
      }
      result(c, r) = result(r, c);
    }
  }
  return result;
}

template<typename Matrix>
Matrix ToFixedFloat(const Matrix& input_mat) {
  Matrix result_mat = input_mat.eval();
  constexpr typename Matrix::Scalar max_limit(pow(2, 19));
  constexpr typename Matrix::Scalar min_limit = -max_limit;
  constexpr typename Matrix::Scalar min_res(pow(2, 10));
  for (int i = 0; i < result_mat.rows(); ++i) {
    for (int j = 0; j < result_mat.cols(); ++j) {
      auto rounded_value =
          static_cast<typename Matrix::Scalar>(static_cast<int64_t>(result_mat(i, j) * min_res))
              / min_res;
      rounded_value = std::min(std::max(rounded_value, min_limit), max_limit);
      result_mat(i, j) = rounded_value;
    }
  }
  return result_mat;
}

}  // namespace vio
}  // namespace dm


#endif  // MATH_MATRIX_BASE_H_
