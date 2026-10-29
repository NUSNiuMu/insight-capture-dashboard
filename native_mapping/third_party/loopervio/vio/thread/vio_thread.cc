#include "vio/thread/vio_thread.h"

#include "glog/logging.h"
namespace dm {
namespace vio {

VioThread::VioThread() {}

void VioThread::Start() {
  thread_ = std::thread(&VioThread::Run, this);
}

void VioThread::Join() {
  RequestStop();
  while (!is_stopped_) {
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
    Notify();
  }
  thread_.join();
}

void VioThread::RequestStop() {
  std::lock_guard<std::mutex> lock(mutex_stop_);
  std::lock_guard<std::mutex> lk(mutex_pause_);
  stop_requested_ = true;
  pause_requested_ = false;
  is_paused_ = false;
}
void VioThread::RequestReset() {
  std::lock_guard<std::mutex> lk(mutex_reset_);
  reset_requested_ = true;
}

void VioThread::RequestPause() {
  std::lock_guard<std::mutex> lk(mutex_pause_);
  pause_requested_ = true;
}

void VioThread::RequestRecovery() {
  std::lock_guard<std::mutex> lk(mutex_pause_);
  pause_requested_ = false;
  is_paused_ = false;
}

void VioThread::Stop() {
  /// to do some options
  is_stopped_ = true;
  stop_requested_ = false;
}
void VioThread::Reset() {
  /// to do some options
  reset_requested_ = false;
}

void VioThread::Pause() {
  is_paused_ = true;
  pause_requested_ = false;
}

bool VioThread::CheckStopRequest() {
  std::lock_guard<std::mutex> lock(mutex_stop_);
  return stop_requested_;
}

bool VioThread::CheckResetRequest() {
  std::lock_guard<std::mutex> lk(mutex_reset_);
  return reset_requested_;
}

bool VioThread::CheckPauseRequest() {
  std::lock_guard<std::mutex> lk(mutex_pause_);
  return pause_requested_;
}

bool VioThread::IsPaused() {
  std::lock_guard<std::mutex> lk(mutex_pause_);
  return is_paused_;
}
bool VioThread::IsStopped() {
  std::lock_guard<std::mutex> lock(mutex_stop_);
  return is_stopped_;
}

void VioThread::Notify() {
  {
    std::lock_guard<std::mutex> lk(run_mtx_);
    is_ready_ = true;
  }
  cond_.notify_all();
}

void VioThread::WaitForNotify() {
  std::unique_lock<std::mutex> lock(run_mtx_);
  cond_.wait(lock, [this] { return is_ready_ || stop_requested_; });
  is_ready_ = false;
}

void VioThread::SetRunningState(bool state) {
  std::lock_guard<std::mutex> lock(running_mtx_);
  is_running_ = state;
}
bool VioThread::IsRunning() {
  std::lock_guard<std::mutex> lock(running_mtx_);
  return is_running_;
}

}  // namespace vio
}  // namespace dm
