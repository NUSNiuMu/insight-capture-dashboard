#ifndef VIO_DATA_MONITOR_H_
#define VIO_DATA_MONITOR_H_

#include <deque>
#include <optional>
#include <map>
#include <sstream>
#include <memory>
#include <iomanip>
#include <string>
#include "vio/thread/vio_thread.h"
#include "common/timer.h"
#include "glog/logging.h"

namespace dm::vio {

struct SensorDataWithTime {
  explicit SensorDataWithTime(int64_t stamp) : sensor_timestamp(stamp) {
    accept_timestamp = common::Timer::TimeStamp();
  }
  int64_t sensor_timestamp;
  int64_t accept_timestamp;
};

struct SensorTimeInfo {
  float fps;
  double one_second_delay;
  double total_delay;
  double waited_time;
};

struct SwSensorDataBuffer {
  std::deque<SensorDataWithTime> queue;
  int64_t base_sensor_timestamp;
  int64_t base_accept_timestamp;
  mutable std::mutex mutex;

  void PushBack(const SensorDataWithTime &data) {
    std::lock_guard<std::mutex> lg(mutex);
    if (queue.empty()) {
      base_sensor_timestamp = data.sensor_timestamp;
      base_accept_timestamp = data.accept_timestamp;
    }
    queue.push_back(data);
    auto front = queue.front();
    auto back = queue.back();
    while (back.sensor_timestamp - front.sensor_timestamp > 1e9) {
      queue.pop_front();
      front = queue.front();
    }
  }

  SensorTimeInfo GetTimeInfo() const {
    std::lock_guard<std::mutex> lg(mutex);
    auto front = queue.front();
    auto back = queue.back();
    auto sensor_time_interval = back.sensor_timestamp - front.sensor_timestamp;
    auto accept_time_interval = back.accept_timestamp - front.accept_timestamp;
    SensorTimeInfo info;
    info.fps = (queue.size() - 1) * 1.0e9 / (back.sensor_timestamp - front.sensor_timestamp);

    info.one_second_delay = 1e-6 * (accept_time_interval - sensor_time_interval);
    auto base_sensor_time_interval = 1e-6 * (back.sensor_timestamp - base_sensor_timestamp);
    auto base_accept_time_interval = 1e-6 * (back.accept_timestamp - base_accept_timestamp);
    info.total_delay = base_accept_time_interval - base_sensor_time_interval;
    info.waited_time = 1e-6 * (common::Timer::TimeStamp() - back.accept_timestamp);
    return info;
  }

  bool IsEmpty() const {
    std::lock_guard<std::mutex> lg(mutex);
    return queue.empty();
  }

  SensorDataWithTime LatestData() const {
    std::lock_guard<std::mutex> lg(mutex);
    return queue.front();
  }
};
class DataMonitor : public VioThread {
 public:
  typedef struct MonitorInfo {
    std::map<std::string, SensorTimeInfo> infos;
    std::map<std::string, float> delay2img0;
    std::string ToString() {
      if (infos.empty()) return std::string();
      std::stringstream ss;
      for (auto &name2info : infos) {
        auto name = name2info.first;
        SensorTimeInfo time_info = name2info.second;
        ss << std::setprecision(4) << name << ": " << "fps: " << time_info.fps << " sd: "
           << time_info.one_second_delay
           << " ad: " << time_info.total_delay << " wt: " << time_info.waited_time;
        if (delay2img0.count(name)) ss << " " << "d2img " << delay2img0[name];
        ss << "\n";
      }
      return ss.str();
    }
  } MonitorInfo;

  using Ptr = std::shared_ptr<DataMonitor>;

  void Run() {
    while (true) {
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

      std::this_thread::sleep_for(std::chrono::milliseconds(1000));
      MonitorInfo monitor_info;

      if (acc_.IsEmpty() || gyr_.IsEmpty() || images_.size() == 0) {
        continue;
      }

      std::unique_lock<std::mutex> ul(image_mutex_);
      monitor_info.infos["acc"] = acc_.GetTimeInfo();
      monitor_info.infos["gyr"] = gyr_.GetTimeInfo();
      for (auto &id2images : images_) {
        auto img_name = "img" + std::to_string(id2images.first);
        monitor_info.infos[img_name] = id2images.second.GetTimeInfo();
      }
      const SwSensorDataBuffer &image0 = images_.begin()->second;
      monitor_info.delay2img0["acc"] = ComputeTimeDelayBetweenSensor(acc_, image0);
      monitor_info.delay2img0["gyr"] = ComputeTimeDelayBetweenSensor(gyr_, image0);

      for (auto &id2images : images_) {
        auto img_name = "img" + std::to_string(id2images.first);
        monitor_info.delay2img0[img_name] = ComputeTimeDelayBetweenSensor(id2images.second, image0);
      }
      std::lock_guard<std::mutex> lg(monitor_info_mutex_);
      monitor_info_ = monitor_info;
    }
  }

  void Flush() {}

  void AcceptAcc(int64_t timestamp) {
    acc_.PushBack(SensorDataWithTime(timestamp));
  }

  void AcceptGyr(int64_t timestamp) {
    gyr_.PushBack(SensorDataWithTime(timestamp));
  }

  void AcceptImage(int cam_id, int64_t timestamp) {
    std::unique_lock<std::mutex> ul(image_mutex_);
    images_[cam_id].PushBack(SensorDataWithTime(timestamp));
  }

  MonitorInfo GetMonitorInfo() {
    std::lock_guard<std::mutex> lg(monitor_info_mutex_);
    return monitor_info_;
  }

 private:
  double ComputeTimeDelayBetweenSensor(const SwSensorDataBuffer &sensor0,
                                       const SwSensorDataBuffer &sensor1) {
    if (sensor0.IsEmpty() || sensor1.IsEmpty()) return -1;
    auto latest0 = sensor0.LatestData();
    auto latest1 = sensor1.LatestData();
    auto sensor_interval = latest0.sensor_timestamp - latest1.sensor_timestamp;
    auto accept_interval = latest0.accept_timestamp - latest1.accept_timestamp;
    return 1e-6 * (accept_interval - sensor_interval);
  }
  std::map<int, SwSensorDataBuffer> images_;

  SwSensorDataBuffer acc_;
  SwSensorDataBuffer gyr_;

  std::mutex image_mutex_;
  std::mutex monitor_info_mutex_;
  MonitorInfo monitor_info_;
};
}  // namespace dm::vio
#endif  // VIO_DATA_MONITOR_H_

