#include "engine.h"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <csignal>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <glog/logging.h>

namespace {
void Read(int fd, void* data, size_t length) {
  auto p = static_cast<char*>(data);
  while (length) {
    auto got = recv(fd, p, length, 0);
    if (got <= 0) throw std::runtime_error("client disconnected");
    p += got; length -= got;
  }
}
void Write(int fd, const void* data, size_t length) {
  auto p = static_cast<const char*>(data);
  while (length) {
    auto sent = send(fd, p, length, MSG_NOSIGNAL);
    if (sent <= 0) throw std::runtime_error("client disconnected");
    p += sent; length -= sent;
  }
}
void Reply(int fd, insight::Json value, const std::vector<cv::Mat>& arrays={}) {
  value["arrays"]=insight::Json::array();
  for(const auto& array:arrays) value["arrays"].push_back({
    {"width",array.cols},{"height",array.rows},{"dtype","float32"}});
  auto bytes = value.dump(); uint32_t size = htonl(bytes.size());
  Write(fd, &size, sizeof(size)); Write(fd, bytes.data(), bytes.size());
  for(const auto& array:arrays) Write(fd,array.data,array.total()*array.elemSize());
}
}
int main(int argc, char** argv) {
  google::InitGoogleLogging(argv[0]); FLAGS_logtostderr = 1;
  cv::setNumThreads(1);
  if (argc != 2) { std::cerr << "usage: insight_mapping SOCKET\n"; return 2; }
  int server = socket(AF_UNIX, SOCK_STREAM, 0);
  sockaddr_un address{}; address.sun_family = AF_UNIX;
  if (strlen(argv[1]) >= sizeof(address.sun_path)) return 2;
  strcpy(address.sun_path, argv[1]); unlink(argv[1]);
  if (bind(server, reinterpret_cast<sockaddr*>(&address), sizeof(address)) || listen(server, 8)) {
    perror("mapping socket"); return 1;
  }
  std::cout << "ready\n" << std::flush;
  insight::Engine engine;
  for (;;) {
    int client = accept(server, nullptr, nullptr);
    if (client < 0) continue;
    try {
      for (;;) {
        uint32_t length; Read(client, &length, sizeof(length)); length = ntohl(length);
        if (length > 1048576) throw std::runtime_error("oversized request");
        std::string text(length, '\0'); Read(client, text.data(), length);
        auto request = insight::Json::parse(text); std::vector<cv::Mat> images;
        for (const auto& spec : request.value("images", insight::Json::array())) {
          int width = spec.at("width"), height = spec.at("height");
          if (width < 0 || height < 0 || width * int64_t(height) > 8388608)
            throw std::runtime_error("invalid image dimensions");
          const auto dtype=spec.at("dtype").get<std::string>();
          if(dtype!="uint8" && dtype!="float32") throw std::runtime_error("invalid array dtype");
          cv::Mat image(height, width, dtype=="uint8" ? CV_8UC1 : CV_32FC1);
          Read(client, image.data, image.total()*image.elemSize());
          images.push_back(std::move(image));
        }
        try {
          auto command = request.at("command").get<std::string>(); insight::Json result;
          if (command == "initialize") result = engine.Initialize(request);
          else if (command == "map") result = engine.Map(request, images);
          else if (command == "localize") result = engine.Localize(request, images);
          else if (command == "reset") { engine.Reset(); result = engine.Stats(); }
          else if (command == "stats") result = engine.Stats();
          else if (command == "geometry") {
            Reply(client, {{"ok",true}}, engine.Geometry()); continue;
          }
          else throw std::runtime_error("unknown command");
          result["ok"] = true; Reply(client, result);
        } catch (const std::exception& error) { Reply(client, {{"ok",false},{"error",error.what()}}); }
      }
    } catch (const std::exception&) { close(client); }
  }
}
