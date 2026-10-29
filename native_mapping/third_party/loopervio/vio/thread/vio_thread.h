#ifndef VIO_THREAD_VIO_THREAD_H_
#define VIO_THREAD_VIO_THREAD_H_

#include <condition_variable>
#include <mutex>
#include <thread>

namespace dm {
namespace vio {
class VioThread {
 public:
  VioThread();

  virtual ~VioThread() = default;

  virtual void Run() = 0;
  virtual void Flush() = 0;

  virtual void Start();
  virtual void Stop();
  virtual void Reset();
  virtual void Pause();
  virtual void Join();

  // Thread Sync
  virtual void RequestStop();
  virtual void RequestReset();
  virtual void RequestPause();
  virtual void RequestRecovery();

  virtual bool CheckStopRequest();
  virtual bool CheckResetRequest();
  virtual bool CheckPauseRequest();

  virtual bool IsPaused();
  virtual bool IsStopped();

  /// finish controller
  bool stop_requested_ = false;
  std::mutex mutex_stop_;

  /// reset controller
  bool reset_requested_ = false;
  std::mutex mutex_reset_;

  /// reset controller
  bool pause_requested_ = false;
  std::mutex mutex_pause_;

  bool is_paused_ = false;
  bool is_stopped_ = false;

  std::mutex running_mtx_;
  bool is_running_ = false;

  void SetRunningState(bool state);
  bool IsRunning();

  virtual void Notify();
  virtual void WaitForNotify();

  bool is_ready_ = false;
  std::mutex run_mtx_;
  std::condition_variable cond_;
  std::thread thread_;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_THREAD_VIO_THREAD_H_
