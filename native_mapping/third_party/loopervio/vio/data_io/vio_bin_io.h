#ifndef VIO_DATA_IO_VIO_BIN_IO_H_
#define VIO_DATA_IO_VIO_BIN_IO_H_
#include <glog/logging.h>
#include <Eigen/Eigen>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>
#include <opencv2/opencv.hpp>
#include "config/sensor_calibration.h"
#include "vio/data_types.h"

namespace dm {
namespace vio {

enum SensorType {
  IMAGE = 0,  // if it's need, append other image types
  CAM_INTRINSIC = 1,
  RIGID = 2,
  IMU_ACC = 3,
  IMU_GYR = 4,
  VIO_FRAME = 5,
  MULTI_VIO_FRAMES = 6,
};

struct SensorData {
  SensorType type;
  uint32_t length = 0;
  uint8_t *data = nullptr;
};

struct NamedSensorData {
  std::string name;
  SensorData sensor_data;
  int id() const {
    return std::stoi(name.substr(0, 1));  // only support 0-9
  }
  SensorType type() const { return sensor_data.type; }
};

class SensorWriter {
 public:
  SensorWriter() {}
  explicit SensorWriter(const std::string &file_name) {
    file_.open(file_name);
    CHECK(file_.is_open()) << "SensorWriter Open file failed, file_name: " << file_name;
  }
  void Write(uint64_t timestamp, const SensorData &data) {
    file_.write(reinterpret_cast<const char *>(&timestamp), sizeof(uint64_t));
    file_.write(reinterpret_cast<const char *>(&data.type), sizeof(SensorType));
    file_.write(reinterpret_cast<const char *>(&data.length), sizeof(uint32_t));
    file_.write(reinterpret_cast<const char *>(data.data), data.length);
    file_.flush();
  }

 private:
  std::ofstream file_;
};

class SensorReader {
 public:
  SensorReader() {}
  explicit SensorReader(const std::string &file_name) { file_.open(file_name); }

  bool Read(uint64_t *timestamp, SensorData *data) {
    file_.read(reinterpret_cast<char *>(timestamp), sizeof(uint64_t));
    if (!file_.good()) return false;
    file_.read(reinterpret_cast<char *>(&(data->type)), sizeof(SensorType));
    if (!file_.good()) return false;
    uint32_t length = 0;
    file_.read(reinterpret_cast<char *>(&length), sizeof(uint32_t));
    if (!file_.good()) return false;
    if (length != data->length && data->data == nullptr) {
      data->data = new uint8_t[length];
    }
    data->length = length;
    file_.read(reinterpret_cast<char *>(data->data), data->length);
    if (!file_.good()) return false;
    return true;
  }

 private:
  std::ifstream file_;
};

// todo: add a txt to record sensor writer name
class VioBinWriter {
 public:
  using Ptr = std::shared_ptr<VioBinWriter>;

  VioBinWriter() {}
  explicit VioBinWriter(const std::string &path) : path_(path + "/") {}

  void WriteSensorCalibration(const SensorCalibration& sensor_calibration) {
    std::ofstream file(path_ + "calibration.json");
    if (file.is_open()) {
      nlohmann::json calib_json;
      sensor_calibration.WriteToJson(&calib_json);
      file << calib_json.dump(4);
      file.close();
    } else {
      CHECK(0) << "Unable to open file for writing the calib json file. " << path_;
    }
  }

  void WriteImage(size_t cam_id, uint64_t stamp, cv::Mat image) {
    std::unique_lock<std::mutex> ul(image_mutex_);
    std::string sensor_name = std::to_string(cam_id) + ".image";
    if (!sensor_writers_.count(sensor_name)) {
      sensor_writers_[sensor_name] = SensorWriter(path_ + sensor_name);
    }
    uint32_t img_size = image.rows * image.cols;
    SensorData sensor_data = {SensorType::IMAGE, img_size, image.data};
    sensor_writers_[sensor_name].Write(stamp, sensor_data);
  }

  void WriteGyr(uint64_t stamp, const Eigen::Vector3d &gyr) {
    std::string sensor_name = "0.gyr";
    if (!sensor_writers_.count(sensor_name)) {
      sensor_writers_[sensor_name] = SensorWriter(path_ + sensor_name);
    }

    const uint8_t *d = reinterpret_cast<const uint8_t *>(gyr.data());
    SensorData sensor_data = {SensorType::IMU_GYR, sizeof(double) * 3, const_cast<uint8_t *>(d)};
    sensor_writers_[sensor_name].Write(stamp, sensor_data);
  }

  void WriteAcc(uint64_t stamp, const Eigen::Vector3d &acc) {
    std::string sensor_name = "0.acc";
    if (!sensor_writers_.count(sensor_name)) {
      sensor_writers_[sensor_name] = SensorWriter(path_ + sensor_name);
    }
    const uint8_t *d = reinterpret_cast<const uint8_t *>(acc.data());
    SensorData sensor_data = {SensorType::IMU_ACC, sizeof(double) * 3, const_cast<uint8_t *>(d)};
    sensor_writers_[sensor_name].Write(stamp, sensor_data);
  }

  void WriteVioFrame(size_t cam_id, uint64_t stamp, const dm::vio::VioProblemFrame& frame) {
    std::string sensor_name = std::to_string(cam_id) + ".frame";
    if (!sensor_writers_.count(sensor_name)) {
      sensor_writers_[sensor_name] = SensorWriter(path_ + sensor_name);
    }
    std::string serialized_data;
    bool state = frame.SerializeToString(&serialized_data);
    CHECK(state) << " Fail to SerializeToString";
    SensorData sensor_data = {SensorType::VIO_FRAME, static_cast<uint32_t>(serialized_data.size()),
                              reinterpret_cast<uint8_t *>(serialized_data.data())};
    sensor_writers_[sensor_name].Write(stamp, sensor_data);
  }

  void WriteMultiVioFrame(uint64_t stamp, const dm::vio::MultiVioProblemFrame& multi_frame) {
    std::string sensor_name = "multi_frame";
    if (!sensor_writers_.count(sensor_name)) {
      sensor_writers_[sensor_name] = SensorWriter(path_ + sensor_name);
    }
    std::string serialized_data;
    bool state = multi_frame.SerializeToString(&serialized_data);
    CHECK(state) << " Fail to SerializeToString";
    SensorData sensor_data = {SensorType::MULTI_VIO_FRAMES,
                              static_cast<uint32_t>(serialized_data.size()),
                              reinterpret_cast<uint8_t *>(serialized_data.data())};
    sensor_writers_[sensor_name].Write(stamp, sensor_data);
  }

 private:
  std::mutex image_mutex_;
  std::unordered_map<std::string, SensorWriter> sensor_writers_;
  std::string path_;
};

class VioBinReader {
 public:
  VioBinReader() {}

  VioBinReader(const std::string& path, size_t cam_num, bool enable_vio_frame,
               const std::string& multi_frame_name = "multi_frame")
      : cam_num_(cam_num),
        path_(path + "/"),
        enable_vio_frame_(enable_vio_frame),
        multi_frame_name_(multi_frame_name) {
    // todo: read sensor name from txt
    for (auto id = 0; id < cam_num_; id++) {
      camera_ids_.push_back(id);
    }
    for (auto idx : camera_ids_) {
      std::string img_name = std::to_string(idx) + ".image";
      sensor_readers_[img_name] = SensorReader(path_ + img_name);
    }
    sensor_readers_["0.acc"] = SensorReader(path_ + "0.acc");
    sensor_readers_["0.gyr"] = SensorReader(path_ + "0.gyr");
    auto FileIsExisted = [&](const std::string &filename) {
      std::ifstream file(path_ + filename);
      return file.good();
    };
    // check frontend format
    if (enable_vio_frame_) {
      if (FileIsExisted(multi_frame_name_)) {
        sensor_readers_[multi_frame_name_] = SensorReader(path_ + multi_frame_name_);
        vio_frame_type_ = SensorType::MULTI_VIO_FRAMES;
      } else {
        bool all_frame_existed = true;
        for (auto idx : camera_ids_) {
          if (!FileIsExisted(std::to_string(idx) + ".frame")) {
            all_frame_existed = false;
          }
        }
        if (!all_frame_existed) {
          enable_vio_frame_ = false;
        } else {
          vio_frame_type_ = SensorType::VIO_FRAME;
          for (auto idx : camera_ids_) {
            sensor_readers_[std::to_string(idx) + ".frame"] =
                SensorReader(path_ + std::to_string(idx) + ".frame");
          }
        }
      }
    }
    for (auto &name2reader : sensor_readers_) {
      uint64_t stamp;
      SensorData data;
      LOG(INFO) << name2reader.first;
      bool state = name2reader.second.Read(&stamp, &data);
      if (state) {
        buffers_.insert(std::make_pair(stamp, NamedSensorData{name2reader.first, data}));
      }
    }
  }

  VioBinReader(const std::string& path, std::vector<uint32_t> cam_ids, bool enable_vio_frame,
               const std::string& multi_frame_name = "multi_frame")
      : camera_ids_(cam_ids),
        path_(path + "/"),
        enable_vio_frame_(enable_vio_frame),
        multi_frame_name_(multi_frame_name) {
    cam_num_ = camera_ids_.size();
    for (auto idx : camera_ids_) {
      std::string img_name = std::to_string(idx) + ".image";
      sensor_readers_[img_name] = SensorReader(path_ + img_name);
    }
    sensor_readers_["0.acc"] = SensorReader(path_ + "0.acc");
    sensor_readers_["0.gyr"] = SensorReader(path_ + "0.gyr");
    auto FileIsExisted = [&](const std::string &filename) {
      std::ifstream file(path_ + filename);
      return file.good();
    };
    // check frontend format
    if (enable_vio_frame_) {
      if (FileIsExisted(multi_frame_name_)) {
        sensor_readers_[multi_frame_name_] = SensorReader(path_ + multi_frame_name_);
        vio_frame_type_ = SensorType::MULTI_VIO_FRAMES;
      } else {
        bool all_frame_existed = true;
        for (auto idx : camera_ids_) {
          if (!FileIsExisted(std::to_string(idx) + ".frame")) {
            all_frame_existed = false;
          }
        }
        if (!all_frame_existed) {
          enable_vio_frame_ = false;
        } else {
          vio_frame_type_ = SensorType::VIO_FRAME;
          for (auto idx : camera_ids_) {
            sensor_readers_[std::to_string(idx) + ".frame"] =
                SensorReader(path_ + std::to_string(idx) + ".frame");
          }
        }
      }
    }
    for (auto &name2reader : sensor_readers_) {
      uint64_t stamp;
      SensorData data;
      LOG(INFO) << name2reader.first;
      bool state = name2reader.second.Read(&stamp, &data);
      if (state) {
        buffers_.insert(std::make_pair(stamp, NamedSensorData{name2reader.first, data}));
      }
    }
  }

  SensorCalibration ReadSensorCalibration() {
    return SensorCalibration(path_ + "calibration.json");
  }

  bool ReadSensorData(uint64_t *stamp, NamedSensorData *data) {
    if (buffers_.empty()) return false;
    auto first_data = *(buffers_.begin());
    *stamp = first_data.first;
    *data = first_data.second;
    auto next_name = first_data.second.name;
    buffers_.erase(buffers_.begin());
    if (!sensor_readers_.count(next_name)) return true;

    auto &sensor_reader = sensor_readers_[next_name];
    uint64_t next_stamp;
    SensorData next_data;
    bool state = sensor_reader.Read(&next_stamp, &next_data);
    if (state) {
      buffers_.insert(std::make_pair(next_stamp, NamedSensorData{next_name, next_data}));
    } else {
      sensor_readers_.erase(next_name);
    }
    return true;
  }

  bool enable_vio_frame() { return enable_vio_frame_; }
  SensorType vio_frame_type() { return vio_frame_type_; }

 private:
  size_t cam_num_;
  std::vector<uint32_t> camera_ids_;
  std::string path_;
  std::unordered_map<std::string, SensorReader> sensor_readers_;
  std::multimap<uint64_t, NamedSensorData> buffers_;
  bool enable_vio_frame_ = false;
  SensorType vio_frame_type_ = SensorType::IMAGE;
  std::string multi_frame_name_;
};

}  // namespace vio
}  // namespace dm

#endif  // VIO_DATA_IO_VIO_BIN_IO_H_
