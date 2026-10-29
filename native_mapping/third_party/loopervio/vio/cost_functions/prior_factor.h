// Copyright 2021 DeepMirror Inc. All rights reserved.

#ifndef VIO_COST_FUNCTIONS_PRIOR_FACTOR_H_
#define VIO_COST_FUNCTIONS_PRIOR_FACTOR_H_

#include <Eigen/Dense>

#include <algorithm>
#include <unordered_map>
#include <utility>
#include <vector>

#include "ceres/ceres.h"
#ifdef CMAKE_BUILD
#include "../ext/rigid.h"
#else
#include "math/rigid.h"
#endif
#include "math/transformation_utils.h"

namespace dm {
namespace vio {

struct MarginalizationData {
  MarginalizationData(ceres::CostFunction* _cost_function, ceres::LossFunction* _loss_function,
                      std::vector<double*> _parameter_blocks, std::vector<int> _marg_set);
  // The address in parameter_blocks is related with _parameter_blocks which are used in a long life
  // cycle, so we do not delete them in the Destructor
  std::vector<double*> parameter_blocks;
  std::vector<int> marg_set;
  std::vector<int> block_sizes;

  std::vector<Eigen::Matrix<double, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor>> jacobians;
  Eigen::VectorXd residuals;
};

struct PriorData {
  ~PriorData();
  void AddMargData(MarginalizationData* marg_data);
  // We marg the points first and then marg the pose, which is faster.
  void Marginalize();
  std::vector<double*> GetMargKeepBlocks(
      const std::vector<std::pair<long, double*>>& all_keep_addr);

  std::vector<MarginalizationData*> marg_datas;
  int marg_size;
  std::unordered_map<long, int> parameter_block_size;
  std::unordered_map<long, int> parameter_block_idx;
  std::unordered_map<long, double*> parameter_block_data;
  int keep_size;
  std::vector<int> keep_block_size;
  std::vector<int> keep_block_idx;
  // The address in keep_block_data are used in a long life cycle,
  // so we do not delete them in the Destructor
  std::vector<double*> keep_block_data;

  Eigen::MatrixXd marg_jacobians;
  Eigen::MatrixXd marg_hessian;
  Eigen::VectorXd marg_residuals;
  Eigen::VectorXd marg_jacobianT_residuals;
};

class PriorFactor : public ceres::CostFunction {
 public:
  explicit PriorFactor(PriorData* prior_data);
  virtual bool Evaluate(double const* const* parameters, double* residuals,
                        double** jacobians) const;
  Eigen::VectorXd CalcResidual(double const* const* parameters);

 private:
  PriorData* prior_data_;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_COST_FUNCTIONS_PRIOR_FACTOR_H_
