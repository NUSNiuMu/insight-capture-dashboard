# Jetson 原生预览界面方案

2026-09-09 调研。网页已默认采用 Babylon camera-facing 粗线；以下为原生客户端的选型建议，尚未实现或部署。

## 建议

优先用 **Qt Widgets + GStreamer 的三路原生视频面板** 验证实际显示收益；如果要把轨迹、模型和控制也完全移出浏览器，采用 **Qt Quick + GStreamer + Qt Quick 3D**。现有后端的录制/回放 API、姿态 WebSocket 和 GLB 资源可以继续使用，界面与 3D 交互需要重新实现。客户端迁移不要求重写 ROS 采集和录制。

| 方案 | 视频接入 | 适用范围 | 本机适配情况 |
| --- | --- | --- | --- |
| Qt Widgets / PyQt5 | GstVideoOverlay 把 NVIDIA sink 嵌入原生窗口 | 最小视频墙原型、控制面板 | 宿主机已有 PyQt5、nv3dsink、nveglglessink；两个 sink 均暴露 GstVideoOverlay，仍需做实际嵌入测试 |
| Qt Quick + Qt Quick 3D | qmlglsink（Qt5）或 qml6glsink（Qt6）共享 GL 纹理 | 完整原生视频、3D 模型/轨迹和交互 | 当前没有 QML sink；Qt6 插件需 GStreamer 1.22 起的支持，不能直接套在现有 1.20.3 栈上 |
| GTK + GStreamer | gtkglsink / gtk4paintablesink | Linux 原生视频和控制界面 | 需新增依赖；当前项目的 Qt/3D 迁移更直接，暂不优先 |

[GstVideoOverlay 官方文档包含 Qt 示例](https://gstreamer.freedesktop.org/documentation/video/gstvideooverlay.html)；[Qt 官方建议复杂 GStreamer 管线直接配合 qml6glsink](https://doc.qt.io/qt-6/qtmultimedia-gstreamer.html)；[GStreamer 1.22 引入 Qt6 QML 视频支持](https://gstreamer.freedesktop.org/releases/1.22/)；[GTK 视频 sink](https://gstreamer.freedesktop.org/documentation/gtk4/index.html)。

## 两条视频路径

1. **先复用现有 H.264 WebRTC**：原生客户端实现现有信令协议，GStreamer `webrtcbin` 收流，接 `rtph264depay ! h264parse ! nvv4l2decoder`，再送 NVIDIA/Qt 视频 sink。这仍使用 H.264，但解码完全绕过浏览器；可以先把客户端改动与后端改动分开测量。现有服务并非 RTSP 地址，不能直接交给普通 RTSP 播放器。
2. **本机优化路径**：在独立预览 worker 中取得原始图像/JPEG，直接解码、转换和显示，省去本机为 WebRTC 做的 H.264 编码再解码。当前 IPC 搬运的是图像数据，并非现成的 NVMM/DMA-BUF 共享接口，需要新增输出/缓冲区共享设计；不能把这一步当作免费零拷贝，也不要另起重型 ROS 图像 reader 与录制竞争。

NVIDIA R36.4.3 文档提供 `nvv4l2decoder ! nv3dsink` 等硬件显示管线：[Accelerated GStreamer](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html)。前一轮 30 帧测试图通过 NVDEC + fakesink，证明原生硬解可用；它尚未证明三路 Qt 显示或零拷贝效果。

## 3D 与性能边界

- Qt Quick 3D 的 RuntimeLoader 支持 glTF/GLB，可以尝试复用头盔和夹爪模型；轨迹可用动态 Geometry。姿态坐标映射、模型层级、关节开合、轨迹清空/代次、录制与回放交互仍需迁移和验证，Babylon JS 代码不能直接作为 Qt Quick 3D 场景运行。[RuntimeLoader](https://doc.qt.io/qt-6/qml-qtquick3d-assetutils-runtimeloader.html)、[QQuick3DGeometry](https://doc.qt.io/qt-6/qquick3dgeometry.html)。
- NVMM、DMA-BUF、GLMemory 不是可以任意直连的同一类型。Qt/GStreamer 必须协商兼容格式并共享 EGL/GL 显示上下文；上下文不一致可能导致失败或每帧 GPU→CPU→GPU 拷贝。需要检查实际协商 caps 与 buffer memory。[qml6glsink](https://gstreamer.freedesktop.org/documentation/qml6/qml6glsink.html)。
- 不建议把硬解帧每帧拉回 NumPy/QImage 后再绘制；这会重新引入 CPU 复制和纹理上传。视频处理留在 GStreamer/C++ 层，Python 可以承担 API、状态和界面协调。
- 原生方案有机会解决浏览器硬解接入障碍，但最终帧率、CPU/GPU 使用和三路同步必须实测，不预先承诺达到 30 FPS。验证时至少检查呈现帧数、帧龄、丢帧、UI 操作响应，以及录制完整性。

## 当前环境

宿主机 PyQt5 可导入，`nv3dsink`、`nveglglessink` 可发现；Dashboard 容器为 GStreamer 1.20.3，未安装 Qt Python 绑定、Qt/GTK 视频 sink 和上述 NVIDIA 显示 sink。正式依赖应进入独立客户端镜像或受控的宿主机安装包；本轮未改系统多媒体栈。
