// Copyright 2022 DeepMirror Inc. All rights reserved.

#ifndef COMMON_TIMER_H_
#define COMMON_TIMER_H_

#include <chrono>
#include <iostream>
#include <memory>
#include <string>

#define TIMER_DECORATOR(func, tag, log_level)              \
  {                                             \
    static dm::common::Timer tag##_timer(#tag); \
    tag##_timer.Tic();                          \
    func;                                       \
    tag##_timer.Toc();                          \
    VLOG(log_level) << tag##_timer.str();             \
  }
#define TIMER_DECORATOR_EVERY_N(func, tag, n)   \
  {                                             \
    static dm::common::Timer tag##_timer(#tag); \
    tag##_timer.Tic();                          \
    func;                                       \
    tag##_timer.Toc();                          \
    LOG_EVERY_N(INFO, n) << tag##_timer.str();  \
  }
namespace dm {
namespace common {

class Timer {
 public:
  using Ptr = std::shared_ptr<Timer>;
  Timer();
  explicit Timer(const std::string& tag);
  void Tic();
  double Toc();
  std::string str();
  double Avg();
  void Reset();
  static int64_t TimeStamp();
  static std::string Format(int64_t stamp, size_t dot = 2);

  double duration() { return duration_; }
  friend std::ostream &operator<<(std::ostream &output, const Timer &timer);

 private:
  size_t count_ = 0;
  double total_duration_ = 0;
  double duration_ = 0;  // in ms
  std::string tag_;
  std::chrono::time_point<std::chrono::steady_clock> start;
  std::chrono::time_point<std::chrono::steady_clock> end;
};

int64_t GetCurrentBootTimestamp();

}  // namespace common
}  // namespace dm

#endif  // COMMON_TIMER_H_
