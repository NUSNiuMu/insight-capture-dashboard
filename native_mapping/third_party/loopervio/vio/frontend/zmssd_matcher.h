#ifndef VIO_FRONTEND_ZMSSD_MATCHER_H_
#define VIO_FRONTEND_ZMSSD_MATCHER_H_

#include <Eigen/Core>
#include <memory>
#include <vector>

#include "common/image_undistortion.h"
#include "vio/frontend/patch_score.h"

#ifdef __ARM_NEON__
#include <arm_neon.h>
#endif

namespace dm {
namespace vio {

using FloatType = double;
using Vector2 = Eigen::Matrix<FloatType, 2, 1>;
using Vector3 = Eigen::Matrix<FloatType, 3, 1>;
using Matrix22 = Eigen::Matrix<FloatType, 2, 2>;

namespace warp {

void getWarpMatrixAffine(const dm::common::ImageUndistortion& ref_image_undistortion,
                         const dm::common::ImageUndistortion& cur_image_undistortion,
                         const cv_mini::Point2f& px_ref, const Vector3& f_ref,
                         const FloatType depth_ref, const dm::math::Rigid3d& T_cur_ref,
                         const int level_ref, Matrix22* A_cur_ref);

void getWarpMatrixAffineHomography(const dm::common::ImageUndistortion& ref_image_undistortion,
                                   const dm::common::ImageUndistortion& cur_image_undistortion,
                                   const cv_mini::Point2f& px_ref, const Vector3& f_ref,
                                   const Vector3& normal_ref, const FloatType depth_ref,
                                   const dm::math::Rigid3d& T_cur_ref, const int level_ref,
                                   Matrix22* A_cur_ref);

int getBestSearchLevel(const Matrix22& A_cur_ref, const int max_level);

bool warpAffine(const Matrix22& A_cur_ref, const cv_mini::Mat& img_ref,
                const cv_mini::Point2f& px_ref, const int level_ref, const int level_cur,
                const int halfpatch_size, uint8_t* patch);

}  // namespace warp

/// Patch-matcher for reprojection-matching and epipolar search in triangulation.
class ZMSSDMatcher {
 public:
  EIGEN_MAKE_ALIGNED_OPERATOR_NEW

  static const int kHalfPatchSize = 4;
  static const int kPatchSize = 8;

  typedef patch_score::ZMSSD<kHalfPatchSize> PatchScore;
  typedef std::shared_ptr<ZMSSDMatcher> Ptr;

  struct Options {
    int align_max_iter =
        10;  //!< number of iterations for aligning the feature patches in gauss newton
    FloatType max_epi_length_optim =
        2.0;  //!< max length of epipolar line to skip epipolar search and directly go to img align
    size_t max_epi_search_steps = 200;  //!< max number of evaluations along epipolar line
    bool subpix_refinement =
        true;  //!< do gauss newton feature patch alignment after epipolar search
    bool affine_est_offset = true;
    bool affine_est_gain = false;
  } options_;

  uint8_t patch_[kPatchSize * kPatchSize] __attribute__((aligned(16)));
  uint8_t patch_with_border_[(kPatchSize + 2) * (kPatchSize + 2)] __attribute__((aligned(16)));
  Matrix22 A_cur_ref_;            //!< affine warp matrix
  Vector2 epi_image_;             //!< vector from epipolar start to end on the image plane
  FloatType epi_length_pyramid_;  //!< length of epipolar line segment in pixels on pyrimid level
                                  //!< (only used for epipolar search)
  int search_level_;

  ZMSSDMatcher() = default;
  ~ZMSSDMatcher() = default;

  /// Find a match by searching along the epipolar line without using any features.
  bool findEpipolarMatchDirect(const std::vector<cv_mini::Mat>& ref_pyr,
                               const dm::common::ImageUndistortion& ref_image_undistortion,
                               const std::vector<cv_mini::Mat>& cur_pyr,
                               const dm::common::ImageUndistortion& cur_image_undistortion,
                               const dm::math::Rigid3d& T_cur_ref, const cv_mini::Point2f& ref_uv,
                               const Vector3& ref_ray, const int ref_level, bool use_same_level,
                               const FloatType d_estimate, const FloatType d_min,
                               const FloatType d_max, cv_mini::Point2f* cur_uv);
  /// search epipolar line between A~C~B for the best match with respect to patch score
  /// the search is done on patch_level, returns image coordinates and best ZMSSD
  void scanEpipolarLine(const std::vector<cv_mini::Mat>& pyr,
                        const dm::common::ImageUndistortion& image_undistortion, const Vector3& A,
                        const Vector3& B, const Vector3& C, const PatchScore& patch_score,
                        const int patch_level, Vector2* image_best, int* zmssd_best);

 private:
  bool align2D_NEON(const cv_mini::Mat& cur_img, uint8_t* ref_patch_with_border, uint8_t* ref_patch,
                    const int n_iter, Vector2* cur_px_estimate);

  bool align2D(const cv_mini::Mat& cur_img, uint8_t* ref_patch_with_border, uint8_t* ref_patch,
               const int n_iter, const bool affine_est_offset, const bool affine_est_gain,
               Vector2* cur_px_estimate, bool no_simd = false,
               std::vector<Eigen::Vector2f>* each_step = nullptr);

  void createPatchFromPatchWithBorder(const uint8_t* const patch_with_border, const int patch_size,
                                      uint8_t* patch) {
    uint8_t* patch_ptr = patch;
    for (int y = 1; y < patch_size + 1; ++y, patch_ptr += patch_size) {
      const uint8_t* ref_patch_border_ptr = patch_with_border + y * (patch_size + 2) + 1;
      for (int x = 0; x < patch_size; ++x) patch_ptr[x] = ref_patch_border_ptr[x];
    }
  }

  /// local optimization for patch_ and patch_with_border_ in *frame* around *px_cur*(image)
  bool findLocalMatch(const std::vector<cv_mini::Mat>& pyr, const int patch_level, Vector2* px_cur);

  /// update best zmssd, if a better one is find, return true
  bool updateZMSSD(const std::vector<cv_mini::Mat>& pyr, const Eigen::Vector2i& pxi,
                   const int patch_level, const PatchScore& patch_score, int* zmssd_best);

  /// check is patch is fully within image
  bool isPatchWithinImage(const dm::common::ImageUndistortion& image_undistortion,
                          const Eigen::Vector2i& pxi, const int patch_level);

  /// search along the epipolar line on image plane
  /// we sample on the UNIT PLANE and check corresponding patches on the image plane
  void scanEpipolarUnitPlane(const std::vector<cv_mini::Mat>& pyr,
                             const dm::common::ImageUndistortion& image_undistortion,
                             const Vector3& A, const Vector3& B, const Vector3& C,
                             const PatchScore& patch_score, const int patch_level,
                             Vector2* image_best, int* zmssd_best);
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_FRONTEND_ZMSSD_MATCHER_H_
