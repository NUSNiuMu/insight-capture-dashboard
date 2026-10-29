// Copyright 2022 DeepMirror Inc. All rights reserved.

#include "common/timer.h"

#include <sstream>
#include <iomanip>

namespace dm {
namespace common {

Timer::Timer() { Tic(); }

Timer::Timer(const std::string& tag) : tag_(tag) { Tic(); }

void Timer::Tic() { start = std::chrono::steady_clock::now(); }

double Timer::Toc() {
  end = std::chrono::steady_clock::now();
  std::chrono::duration<double> elapsed_seconds = end - start;
  auto duration = elapsed_seconds.count() * 1000;
  total_duration_ += duration;
  duration_ = duration;
  count_++;
  return duration;
}

int64_t Timer::TimeStamp() {
  std::chrono::nanoseconds ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch());
  return ns.count();
}

std::string Timer::Format(int64_t stamp, size_t dot) {
  std::stringstream ss;
  ss << std::fixed << std::setprecision(dot) << stamp * 1e-9;
  return ss.str();
}


std::ostream& operator<<(std::ostream& output, const Timer& timer) {
  output << "Timer " << timer.tag_ << ": " << timer.duration_ << "ms "
         << timer.total_duration_ / timer.count_ << "ms(avg)";
  return output;
}

std::string Timer::str() {
  std::stringstream ss;
  ss << "Timer " << tag_ << ": " << duration_ << "ms " << total_duration_ / count_ << "ms(avg)";
  return ss.str();
}

double Timer::Avg() {
  return count_ > 0 ? total_duration_ / count_ : 0;
}

void Timer::Reset() {
  count_ = 0;
  total_duration_ = 0;
  duration_ = 0;
}

#ifdef __APPLE__
int64_t GetCurrentBootTimestamp() {
    return 0;
}
#else
int64_t GetCurrentBootTimestamp() {
  struct timespec time;
  clock_gettime(CLOCK_BOOTTIME, &time);
  int64_t cur_ts =
      static_cast<int64_t>(time.tv_sec * 1000000000) + static_cast<int64_t>(time.tv_nsec);
  return cur_ts;
}
#endif

}  // namespace common
}  // namespace dm
