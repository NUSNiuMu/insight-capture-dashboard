#include "superglue.h"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <cstring>
#include <cmath>
#include <map>
#include <stdexcept>
#include <nlohmann/json.hpp>

namespace insight {
namespace {
void Transfer(int fd, void* data, size_t size, bool writing) {
  auto p = static_cast<char*>(data);
  while (size) {
    auto n = writing ? send(fd, p, size, MSG_NOSIGNAL) : recv(fd, p, size, 0);
    if (n <= 0) throw InferenceUnavailable("SuperGlue inference connection failed");
    p += n;
    size -= n;
  }
}
using Pixel = std::pair<int, int>;
Pixel Identity(const cv::Point2f& point) {
  return {std::lround(point.x * 16), std::lround(point.y * 16)};
}
}

std::vector<dm::vio::lm::MapPoint*> MatchReference(
    dm::vio::lm::KeyFrame* query, dm::vio::lm::KeyFrame* reference) {
  const auto qcam = query->master_cam_id(), rcam = reference->master_cam_id();
  auto left = query->MatchingImage();
  auto right = reference->MatchingImage();
  if (left.type() != CV_8UC1 || left.size() != right.size())
    throw std::runtime_error("SuperGlue localization requires same-sized grayscale images");
  int fd = socket(AF_UNIX, SOCK_STREAM, 0);
  if (fd < 0) throw InferenceUnavailable("Cannot create SuperGlue inference connection");
  struct Connection { int fd; ~Connection() { close(fd); } } connection{fd};
  timeval timeout{30, 0};
  setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  std::strcpy(address.sun_path, "/run/superglue/matcher.sock");
  if (connect(fd, reinterpret_cast<sockaddr*>(&address), sizeof(address)))
    throw InferenceUnavailable("Cannot reach SuperGlue inference socket");
  nlohmann::json metadata = {{"command", "match"}, {"height", left.rows},
    {"width", left.cols}, {"payload_sizes", {left.total(), right.total()}}};
  auto text = metadata.dump();
  uint32_t length = htonl(text.size());
  Transfer(fd, &length, 4, true);
  Transfer(fd, text.data(), text.size(), true);
  Transfer(fd, left.data, left.total(), true);
  Transfer(fd, right.data, right.total(), true);
  Transfer(fd, &length, 4, false);
  length = ntohl(length);
  if (!length || length > 1'000'000) throw std::runtime_error("Invalid SuperGlue response header");
  text.resize(length);
  Transfer(fd, text.data(), length, false);
  metadata = nlohmann::json::parse(text);
  if (!metadata.at("ok").get<bool>()) throw InferenceUnavailable(metadata.at("error").get<std::string>());
  const size_t count = metadata.at("matches");
  const size_t dim = metadata.at("descriptor_dim");
  std::vector<size_t> sizes = metadata.at("payload_sizes");
  if (count > 4096 || dim != 256 || sizes != std::vector<size_t>{count*8,count*8,count*dim*4,count*4})
    throw std::runtime_error("Invalid SuperGlue match arrays");
  std::vector<cv::Point2f> qp(count), rp(count);
  Transfer(fd, qp.data(), sizes[0], false);
  Transfer(fd, rp.data(), sizes[1], false);
  std::vector<char> unused(sizes[2]+sizes[3]);
  Transfer(fd, unused.data(), unused.size(), false);
  std::map<Pixel, size_t> query_ids, reference_ids;
  for (size_t i=0; i<query->FeatureSize(qcam); ++i)
    query_ids.emplace(Identity(query->key_points(qcam)[i].pt), i);
  for (size_t i=0; i<reference->FeatureSize(rcam); ++i)
    reference_ids.emplace(Identity(reference->key_points(rcam)[i].pt), i);
  std::vector<dm::vio::lm::MapPoint*> matched(query->FeatureSize(qcam), nullptr);
  for (size_t i=0; i<count; ++i) {
    auto q=query_ids.find(Identity(qp[i])), r=reference_ids.find(Identity(rp[i]));
    if (q==query_ids.end() || r==reference_ids.end()) continue;
    auto point=reference->GetMapPoint(rcam, r->second);
    if (point && !point->IsBad() && point->NumOfObs()>=3 && point->state().refined)
      matched[q->second]=point;
  }
  return matched;
}
}
