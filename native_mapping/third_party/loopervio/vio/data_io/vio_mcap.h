// Copyright 2024 DeepMirror Inc. All rights reserved.

#ifndef VIO_DATA_IO_VIO_MCAP_H_
#define VIO_DATA_IO_VIO_MCAP_H_

#ifndef DISABLE_VIO_MCAP
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

// online mcap server is currently only supported on x86_64 as it requires boost libraries
#ifdef __x86_64__
#include "foxglove/websocket/base64.hpp"
#include "foxglove/websocket/server_factory.hpp"
#include "foxglove/websocket/websocket_notls.hpp"
#include "foxglove/websocket/websocket_server.hpp"
#endif  // __x86_64__

#include "google/protobuf/descriptor.h"
#include "google/protobuf/descriptor.pb.h"

#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#include "mcap/writer.hpp"

#endif  // DISABLE_VIO_MCAP

#include "vio/backend/frame_state.h"
#include "vio/data_types.h"
#include "vio/vio_system_status.h"

namespace dm::vio {

#ifndef DISABLE_VIO_MCAP

using mcap::McapWriter;

class VIOMcapWriter {
 public:
  ~VIOMcapWriter();

  void Initialize(bool is_enable = false);

  void DumpDouble(std::string_view channel_name, uint64_t timestamp, double data);
  void DumpBoolean(std::string_view channel_name, uint64_t timestamp, bool data);
  void DumpInteger(std::string_view channel_name, uint64_t timestamp, double data);
  void DumpString(std::string_view channel_name, uint64_t timestamp, std::string_view data);
  // The difference to the last input to the channel will be computed and dumped. The first
  // increment is zero.
  void DumpSequenceAsIncrement(std::string_view channel_name, uint64_t timestamp, double data);
  void DumpVector3d(std::string_view channel_name, uint64_t timestamp, const Eigen::Vector3d& data);
  // error_level: {UNKNOWN = 0; DEBUG = 1; INFO = 2; WARNING = 3; ERROR = 4; FATAL = 5;}
  void DumpLog(std::string_view channel_name, uint64_t timestamp, int error_level,
               const std::string& log, const std::string& file_name = "", int line_number = 0);
  void DumpVioSystemStatus(uint64_t timestamp, dm::vio::VioStatusInfo info);
  void DumpFrontEndDetectPoint(uint64_t timestamp, dm::vio::CameraId camera_id,
                               size_t tracking_observation_num, size_t detect_new_observation_num);
  void DumpIMUPropagation(dm::vio::FrameMeta::Ptr frame_meta);

  void DumpFeatures(uint64_t timestamp, size_t msckf_features_num, size_t slam_features_num,
                    size_t slam_init_features_num);
  void DumpImagePropagation(dm::vio::FrameMeta::Ptr frame_meta);
  void DumpOutputPose(uint64_t timestamp, const dm::vio::FrameState& state);
  void DumpPose(std::string_view channel_name, uint64_t timestamp, const dm::math::Rigid3d& pose);
  void DumpVelocityAndBiases(std::string_view channel_name, uint64_t timestamp,
                             const Eigen::Vector3d& velocity, const Eigen::Vector3d& acc_bias,
                             const Eigen::Vector3d& gyro_bias);

  void DumpImage(std::string_view channel_name, uint64_t timestamp, const cv::Mat& image);

  static VIOMcapWriter& Instance() {
    static VIOMcapWriter instance;
    return instance;
  }

 private:
  struct MessagePayload {
    std::string channel_name;
    std::string schema_key;
    std::string encoding;
    mcap::Message message;
    std::string payload;
  };

  VIOMcapWriter();

  bool IsActive() const {
#ifdef __x86_64__
    return writer_ != nullptr || server_ != nullptr;
#else
    return writer_ != nullptr;
#endif  // __x86_64__
  }

  void InitializeWriter();

  void InitializeServer();

  google::protobuf::FileDescriptorSet BuildFileDescriptorSet(
      const google::protobuf::Descriptor* toplevelDescriptor) const;

  template <class PROTO_T>
  const std::string& GetSchemaKey(const PROTO_T& /* proto */) {
    auto descriptor = PROTO_T::descriptor();
    if (auto iter = schemas_.find(descriptor->full_name()); iter != schemas_.end()) {
      return iter->first;
    }

    const auto& [iter, _] =
        schemas_.emplace(descriptor->full_name(),
                         mcap::Schema(descriptor->full_name(), "protobuf",
                                      BuildFileDescriptorSet(descriptor).SerializeAsString()));
    return iter->first;
  }

  template <class PROTO_T>
  void DumpChannelProto(const std::string_view& channel_name, uint64_t timestamp,
                        const PROTO_T& proto) {
    static_assert(std::is_convertible<PROTO_T*, google::protobuf::Message*>::value,
                  "The input must be of a proto type.");
    if (!IsActive()) {
      return;
    }

    mcap::Message msg;
    // TODO(yuance): make them configurable
    msg.publishTime = timestamp;
    msg.logTime = timestamp;
    MessagePayload msg_payload = {
        .channel_name = std::string(channel_name),
        .schema_key = GetSchemaKey(proto),
        .encoding = "protobuf",
        .message = msg,
        .payload = proto.SerializeAsString(),
    };
    {
      std::unique_lock<std::mutex> lk(queue_mutex_);
      message_queue_.push(std::move(msg_payload));
    }
    message_queue_cv_.notify_one();
  }

  template <class SOURCE_T, class PROTO_T>
  void DumpChannelProto(const std::string_view& channel_name, uint64_t timestamp,
                        const SOURCE_T& source, std::function<PROTO_T(const SOURCE_T&)> converter) {
    if (!IsActive()) {
      return;
    }
    PROTO_T proto = converter(source);
    DumpChannelProto(channel_name, timestamp, proto);
  }

  std::unique_ptr<McapWriter> writer_;
  // proto_descriptor -> schema
  std::unordered_map<std::string, mcap::Schema> schemas_;
  // channel_name -> channel_id
  std::unordered_map<std::string, mcap::ChannelId> writer_channel_name_2_id_;
  std::mutex queue_mutex_;
  // message -> channel_name
  std::queue<MessagePayload> message_queue_;
  std::thread dumper_thread_;
  std::condition_variable message_queue_cv_;
  // Flag to stop the thread.
  std::atomic_bool stop_;

#ifdef __x86_64__
  // foxglove server
  std::unique_ptr<foxglove::ServerInterface<websocketpp::connection_hdl>> server_;
  std::unordered_map<std::string, mcap::ChannelId> server_channel_name_2_id_;
#endif  // __x86_64__
};

#else  // DISABLE_VIO_MCAP

class VIOMcapWriter {
 public:
  void Initialize(bool /* is_enable */ = false) {}

  void DumpDouble(std::string_view channel_name, uint64_t timestamp, double data) {}
  void DumpBoolean(std::string_view channel_name, uint64_t timestamp, bool data) {}
  void DumpInteger(std::string_view channel_name, uint64_t timestamp, double data) {}
  void DumpString(std::string_view channel_name, uint64_t timestamp, std::string_view data) {}
  void DumpSequenceAsIncrement(std::string_view channel_name, uint64_t timestamp, double data) {}
  void DumpVector3d(std::string_view channel_name, uint64_t timestamp,
                    const Eigen::Vector3d& data) {}
  void DumpLog(std::string_view channel_name, uint64_t timestamp, int error_level,
               const std::string& log) {}
  void DumpVioSystemStatus(uint64_t timestamp, dm::vio::VioStatusInfo info) {}
  void DumpFrontEndDetectPoint(uint64_t timestamp, dm::vio::CameraId camera_id,
                               size_t tracking_observation_num, size_t detect_new_observation_num) {
  }
  void DumpIMUPropagation(dm::vio::FrameMeta::Ptr frame_meta) {}

  void DumpFeatures(uint64_t timestamp, size_t msckf_features_num, size_t slam_features_num,
                    size_t slam_init_features_num) {}
  void DumpImagePropagation(dm::vio::FrameMeta::Ptr frame_meta) {}
  void DumpOutputPose(uint64_t timestamp, const dm::vio::FrameState& frame_pose) {}
  void DumpPose(std::string_view channel_name, uint64_t timestamp, const dm::math::Rigid3d& pose) {}
  void DumpVelocityAndBiases(std::string_view channel_name, uint64_t timestamp,
                             const Eigen::Vector3d& velocity, const Eigen::Vector3d& acc_bias,
                             const Eigen::Vector3d& gyro_bias);
  void DumpImage(std::string_view channel_name, uint64_t timestamp, const cv::Mat& image) {}

  static VIOMcapWriter& Instance() {
    static VIOMcapWriter instance;
    return instance;
  }

 private:
  VIOMcapWriter() {}
};

#endif  // DISABLE_VIO_MCAP

}  // namespace dm::vio

#endif  // VIO_DATA_IO_VIO_MCAP_H_
