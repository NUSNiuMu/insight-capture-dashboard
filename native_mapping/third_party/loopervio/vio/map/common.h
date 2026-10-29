// Copyright 2020 DeepMirror Inc. All rights reserved.
#ifndef VIO_MAP_COMMON_H_
#define VIO_MAP_COMMON_H_

namespace dm::vio::lm {

enum CameraRole {
  kMaster = 0,
  kSlave = 1,
  kStandalone = 2,
};
}  // namespace dm::vio::lm

#endif  // VIO_MAP_COMMON_H_
