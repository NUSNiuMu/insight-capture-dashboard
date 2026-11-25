// Capture unchanged JPEGs, V4L2 timestamps, UVC headers, and raw IMU XU packets.
// No firmware, external-trigger, calibration, or other vendor SET commands.
#include <linux/uvcvideo.h>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <poll.h>
#include <fcntl.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <exception>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <mutex>
#include <vector>

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
volatile std::sig_atomic_t interrupted = 0;
void on_signal(int) { interrupted = 1; }
uint64_t now_ns() {
  timespec ts{};
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return uint64_t(ts.tv_sec) * 1000000000ULL + ts.tv_nsec;
}
int control(int fd, unsigned long request, void* value) {
  int ret;
  do { ret = ioctl(fd, request, value); } while (ret < 0 && errno == EINTR);
  return ret;
}
void require(bool success, const std::string& operation) {
  if (!success) throw std::runtime_error(operation + ": " + std::strerror(errno));
}
std::string read_text(const fs::path& p) {
  std::ifstream in(p);
  std::string value;
  std::getline(in, value);
  return value;
}
fs::path usb_device(const std::string& name) {
  auto node = fs::canonical(name).filename();
  auto p = fs::canonical(fs::path("/sys/class/video4linux") / node / "device");
  while (!fs::exists(p / "idVendor") && p != p.root_path()) p = p.parent_path();
  if (read_text(p / "idVendor") != "1bcf" ||
      read_text(p / "idProduct") != "28c4")
    throw std::runtime_error("USB identity does not match the validated YLX camera");
  return p;
}

struct Queue {
  int fd = -1;
  v4l2_buf_type type;
  std::vector<void*> buffers;
  std::vector<size_t> lengths;
  bool streaming = false;
  Queue(const std::string& device, v4l2_buf_type kind) : type(kind) {
    fd = open(device.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
    require(fd >= 0, "open " + device);
  }
  Queue(const Queue&) = delete;
  Queue& operator=(const Queue&) = delete;
  void allocate() {
    v4l2_requestbuffers r{};
    r.count = 6;
    r.type = type;
    r.memory = V4L2_MEMORY_MMAP;
    require(control(fd, VIDIOC_REQBUFS, &r) == 0, "VIDIOC_REQBUFS");
    if (r.count == 0) throw std::runtime_error("No V4L2 buffers allocated");
    for (unsigned i = 0; i < r.count; ++i) {
      v4l2_buffer b{};
      b.type = type;
      b.memory = V4L2_MEMORY_MMAP;
      b.index = i;
      require(control(fd, VIDIOC_QUERYBUF, &b) == 0, "VIDIOC_QUERYBUF");
      void* ptr = mmap(nullptr, b.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, b.m.offset);
      require(ptr != MAP_FAILED, "mmap");
      buffers.push_back(ptr);
      lengths.push_back(b.length);
      requeue(b);
    }
  }
  void start() {
    require(control(fd, VIDIOC_STREAMON, &type) == 0, "VIDIOC_STREAMON");
    streaming = true;
  }
  bool dequeue(v4l2_buffer* b) {
    *b = {};
    b->type = type;
    b->memory = V4L2_MEMORY_MMAP;
    if (control(fd, VIDIOC_DQBUF, b) < 0) {
      if (errno == EAGAIN) return false;
      require(false, "VIDIOC_DQBUF");
    }
    if (b->index >= buffers.size() || b->bytesused > lengths[b->index])
      throw std::runtime_error("V4L2 buffer bounds invalid");
    return true;
  }
  void requeue(v4l2_buffer& b) {
    require(control(fd, VIDIOC_QBUF, &b) == 0, "VIDIOC_QBUF");
  }
  ~Queue() {
    if (streaming) control(fd, VIDIOC_STREAMOFF, &type);
    for (size_t i = 0; i < buffers.size(); ++i) munmap(buffers[i], lengths[i]);
    if (fd >= 0) close(fd);
  }
};

// Pipe protocol: little-endian uint32 type/length, uint64 timestamp/auxiliary,
// followed by payload. JPEG and UVC metadata use the buffer sequence as auxiliary.
// IMU packets carry request end and request start on CLOCK_MONOTONIC.
// A single mutex preserves framing across the independent video/IMU threads.
std::mutex pipe_mutex;
void stream_packet(uint32_t type, uint64_t stamp, uint64_t auxiliary,
                   const uint8_t* data, uint32_t length) {
  std::lock_guard<std::mutex> lock(pipe_mutex);
  uint64_t header[3] = {uint64_t(type) | (uint64_t(length) << 32), stamp, auxiliary};
  auto write_all = [](const void* ptr, size_t size) {
    auto bytes = static_cast<const uint8_t*>(ptr);
    while (size) {
      ssize_t count = write(STDOUT_FILENO, bytes, size);
      if (count < 0 && errno == EINTR) continue;
      require(count > 0, "write live transport");
      bytes += count;
      size -= count;
    }
  };
  write_all(header, sizeof(header));
  write_all(data, length);
}

void capture(const std::string& video_path, const std::string& meta_path,
             unsigned fps, unsigned width, unsigned height) {
  if (usb_device(video_path) != usb_device(meta_path))
    throw std::runtime_error("Video and metadata must belong to the same USB camera");
  Queue video(video_path, V4L2_BUF_TYPE_VIDEO_CAPTURE);
  Queue meta(meta_path, V4L2_BUF_TYPE_META_CAPTURE);
  v4l2_format fmt{};
  fmt.type = video.type;
  fmt.fmt.pix.width = width;
  fmt.fmt.pix.height = height;
  fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
  require(control(video.fd, VIDIOC_S_FMT, &fmt) == 0, "set video format");
  if (fmt.fmt.pix.width != width || fmt.fmt.pix.height != height ||
      fmt.fmt.pix.pixelformat != V4L2_PIX_FMT_MJPEG)
    throw std::runtime_error("Requested MJPEG mode unavailable");
  v4l2_streamparm parm{};
  parm.type = video.type;
  parm.parm.capture.timeperframe = {1, fps};
  require(control(video.fd, VIDIOC_S_PARM, &parm) == 0, "set fps");
  const auto period = parm.parm.capture.timeperframe;
  if (!period.numerator || uint64_t(period.numerator) * fps != period.denominator)
    throw std::runtime_error("Negotiated USB frame rate differs from profile");
  fmt = {};
  fmt.type = meta.type;
  require(control(meta.fd, VIDIOC_G_FMT, &fmt) == 0, "get metadata format");
  if (fmt.fmt.meta.dataformat != V4L2_META_FMT_UVC)
    throw std::runtime_error("Expected UVC metadata node");
  video.allocate();
  meta.allocate();
  meta.start();
  video.start();

  std::atomic<bool> stop{false};
  std::exception_ptr imu_error;
  std::thread imu([&] {
    try {
      uint8_t length[2]{};
      uvc_xu_control_query query{3, 1, 0x85, 2, length};
      require(control(video.fd, UVCIOC_CTRL_QUERY, &query) == 0, "IMU GET_LEN");
      if (length[0] != 128 || length[1]) throw std::runtime_error("Unknown UQ2168 IMU format");
      uint8_t packet[128];
      query = {3, 1, 0x81, sizeof(packet), packet};
      while (!stop && !interrupted) {
        auto start = Clock::now();
        auto before = now_ns();
        require(control(video.fd, UVCIOC_CTRL_QUERY, &query) == 0, "IMU GET_CUR");
        stream_packet(2, now_ns(), before, packet, sizeof(packet));
        std::this_thread::sleep_until(start + std::chrono::milliseconds(5));
      }
    } catch (...) { imu_error = std::current_exception(); stop = true; }
  });
  std::exception_ptr video_error;
  try {
    while (!stop && !interrupted) {
      pollfd fds[] = {{video.fd, POLLIN, 0}, {meta.fd, POLLIN, 0}};
      const int ret = poll(fds, 2, 500);
      if (ret < 0 && errno == EINTR) continue;
      require(ret >= 0, "poll");
      for (int i = 0; i < 2; ++i) {
        if (fds[i].revents & (POLLERR | POLLHUP | POLLNVAL))
          throw std::runtime_error("UQ2168 disconnected");
        if (!(fds[i].revents & POLLIN)) continue;
        Queue& q = i == 0 ? video : meta;
        v4l2_buffer b{};
        if (!q.dequeue(&b)) continue;
        auto* data = static_cast<const uint8_t*>(q.buffers[b.index]);
        if (b.bytesused && !(b.flags & V4L2_BUF_FLAG_ERROR)) {
          if (i == 0 && (b.flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) != V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC)
            throw std::runtime_error("Video requires monotonic V4L2 timestamps");
          if (i == 1 || (b.bytesused >= 4 && data[0] == 0xff && data[1] == 0xd8)) {
            const uint64_t stamp = i == 0 ? uint64_t(b.timestamp.tv_sec) * 1000000000 +
                                           uint64_t(b.timestamp.tv_usec) * 1000 : now_ns();
            stream_packet(i == 0 ? 1 : 3, stamp, b.sequence, data, b.bytesused);
          }
        }
        q.requeue(b);
      }
    }
  } catch (...) { video_error = std::current_exception(); }
  stop = true;
  imu.join();
  if (imu_error) std::rethrow_exception(imu_error);
  if (video_error) std::rethrow_exception(video_error);
}

int main(int argc, char** argv) {
  try {
    if (argc != 6) throw std::runtime_error("Usage: uq2168_stream VIDEO META FPS WIDTH HEIGHT");
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGPIPE, SIG_IGN);
    capture(argv[1], argv[2], std::stoul(argv[3]), std::stoul(argv[4]), std::stoul(argv[5]));
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
  return 0;
}
