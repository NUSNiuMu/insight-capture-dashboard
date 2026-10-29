#ifndef VIO_IMAGE_ALIGNMENT_H_
#define VIO_IMAGE_ALIGNMENT_H_

#include <map>
#include <memory>
#include <utility>
#include <vector>
#include "glog/logging.h"
#include "opencv2/opencv.hpp"
#include "vio/naive_vio.h"
#include "vio/thread/vio_thread.h"

namespace dm::vio {

class ImageAlignment : public VioThread {
 public:
  using Ptr = std::shared_ptr<ImageAlignment>;
  explicit ImageAlignment(int cv_rng_seed = -1) : cv_rng_seed_(cv_rng_seed) {}
  void Join() override {
    RequestStop();
    cond_.notify_all();
    thread_.join();
  }

  void Run() {
    pthread_setname_np(pthread_self(), "vio_frontend");
    if (cv_rng_seed_ >= 0) {
      cv::setRNGSeed(cv_rng_seed_);
    }
    while (true) {
      std::unique_lock<std::mutex> ul(image_mutex_);
      cond_.wait(ul, [&] {
        return CheckStopRequest() ||
               (!image_buffer_.empty() &&
                (image_buffer_.begin()->second.size() == 2 || image_buffer_.size() > 1));
      });
      if (CheckResetRequest()) {
        Reset();
      }
      if (CheckStopRequest()) {
        Stop();
        break;
      }
      if (CheckPauseRequest()) {
        Pause();
      }
      while (IsPaused()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }

      auto it = image_buffer_.begin();
      if (image_buffer_.size() > 1) {
        image_buffer_.erase(it);
        continue;
      }

      uint64_t timestamp = it->first;
      std::vector<std::pair<int, cv::Mat>> stereo_images;
      stereo_images.swap(it->second);
      image_buffer_.erase(it);
      ul.unlock();  // When running offline data, this line needs to be commented out to prevent the
                    // data from being quickly flushed away.

      if (vio_) {
        int order = stereo_images[0].first - stereo_images[1].first;
        switch (order) {
          case 0:
            CHECK(0) << "The timestamp " << timestamp << " has two images with camera_id "
                     << stereo_images[0].first;
            break;
          case 1:
            vio_->AddImage(stereo_images[1].first, timestamp, stereo_images[1].second);
            vio_->AddImage(stereo_images[0].first, timestamp, stereo_images[0].second);
            break;
          case -1:
            vio_->AddImage(stereo_images[0].first, timestamp, stereo_images[0].second);
            vio_->AddImage(stereo_images[1].first, timestamp, stereo_images[1].second);
            break;
          default:
            CHECK(0) << "Wrong camera id: " << it->second[0].first << " " << it->second[1].first;
            break;
        }
      }
    }
  }

  void Flush() {}

  void SetVioHandle(void* handle) {
    CHECK_NE(handle, nullptr);
    vio_ = reinterpret_cast<NaiveVio*>(handle);
  }

  void AddImage(const uint32_t camera_id, const uint64_t timestamp, const cv::Mat& image) {
    std::unique_lock<std::mutex> ul(image_mutex_);
    uint64_t image_timestamp = std::abs(static_cast<int64_t>(timestamp) - last_ts_) > 3 * 1e6
                                   ? timestamp
                                   : static_cast<uint64_t>(last_ts_);
    last_ts_ = timestamp;
    image_buffer_[image_timestamp].push_back(std::make_pair(camera_id, image.clone()));
    ul.unlock();
    cond_.notify_one();
  }

 private:
  std::mutex image_mutex_;
  std::map<uint64_t, std::vector<std::pair<int, cv::Mat>>> image_buffer_;
  std::condition_variable cond_;
  int64_t last_ts_ = 0;
  NaiveVio* vio_;
  int cv_rng_seed_ = -1;
};
}  // namespace dm::vio
#endif  // VIO_IMAGE_ALIGNMENT_H_
