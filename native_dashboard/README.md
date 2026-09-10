# Jetson 原生采集台试验

Qt Quick Controls 控制界面、Qt Quick 3D 模型/轨迹、GStreamer H.264 硬件解码的独立客户端。复用现有 Dashboard HTTP/WebSocket 和 WebRTC 服务，不新增 ROS 图像订阅。

当前为 `experiment/qt-native-dashboard` 分支上的采集主界面原型。主仓库网页仍可远程访问，不修改后端启动方式，也未替换 kiosk 开机入口。

## 构建与启动

本机验证环境：Jetson Orin NX、JetPack R36.4.3、Ubuntu 22.04、GStreamer 1.20、Qt 6.2.4、X11。

```bash
./native_dashboard/run.sh
```

不带参数时默认连接本机 `http://127.0.0.1:8765`，以 30 FPS 目标全屏启动；缺少可执行文件或 Qt 运行库时自动构建。Dashboard 后端需保持运行。传入参数时按指定参数启动。

构建需要 Docker 和本机 Jetson Multimedia API 头文件。Qt 在独立 Ubuntu 22.04 构建容器内安装，运行库导出到 `.native-build/runtime`；运行时使用宿主机已有的 NVIDIA/GStreamer 驱动，避免替换 JetPack 多媒体栈。非标准 SDK 目录可通过 `JETSON_MM_HEADERS` 指定。

`--server` 可以指定后端地址；WebRTC 端口使用相机 API 返回值，媒体连接仍需要 ICE 可达。当前客户端使用 Jetson 专用解码器，尚未适配普通 PC。

`--fps 25` / `--fps 30` 设置预览目标，也可在界面顶部切换。它控制服务端请求目标，不保证最终显示率。

```bash
native_dashboard/run.sh --fps 30 --quit-after 60 --diagnostics /tmp/insight-native.json
```

诊断文件包含连接状态、姿态可见性、模型载入结果、轨迹点数、各路视频累计输出/丢弃计数和输出 FPS。视频计数取自 GstBaseSink，表示输出端接收并渲染的 buffer，不能视为显示器扫描输出次数。3D FPS 是 Qt 场景实际重绘率；静止场景按需绘制。

## 已接入的功能

- 三路现有 WebRTC 视频：`webrtcbin → rtph264depay → h264parse → nvv4l2decoder → nv3dsink`，使用 NVMM 硬件解码结果。
- 实时姿态、三模型、夹爪开合、轨迹显示/保留/清除；支持鼠标旋转、平移、缩放。遵循后端姿态可见性和轨迹 generation/sequence；断线隐藏过期姿态并重连获取快照。
- GLB 模型缓存；为 Qt 6.2 转换 `KHR_mesh_quantization` 和 `EXT_texture_webp`，保持原模型内容。
- 任务进入/结束、录制启停、作废最近一条、采集检查、新建地图。
- 已准备 MP4 回放、暂停/继续、时间轴拖动、视频时间驱动的姿态和轨迹、返回实时。回放使用录制库的稳定 bag ID。
- 录制列表仅在首次打开、手动刷新和停止录制后读取，避免反复扫描录制目录。

## 实现边界

视频采用嵌入 Qt 窗口的原生子窗口。这使当前 JetPack 可以直接使用 NVIDIA sink，但不是共享 QML 纹理：不支持任意 QML 遮罩/旋转和覆盖层。弹出菜单与确认框打开时暂时隐藏视频子窗口，避免遮挡。

轨迹使用 Quick 3D 动态线段几何，位置在三维空间，能够随视角旋转；当前是细线。姿态坐标使用 ROS → Qt 右手坐标转换，米换算为厘米。

这是采集/回放主界面的试验，不覆盖网页的数据集导出、标注、设备设置等全部页面，尚未作为客户发布入口。长时间运行、断电恢复和现场录制质量仍需后续验收。

## 验证原则

真实视频与只读姿态连接现有设备；录制、任务和地图的自动化操作使用独立模拟后端，不对现场执行录制或地图重置。比较性能时关闭旧浏览器，保留后端和相机，分别采样服务端编码计数与原生输出计数，并排除启动、退出与会话重置区间。

参考：[NVIDIA Accelerated GStreamer](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html)、[GstVideoOverlay](https://gstreamer.freedesktop.org/documentation/video/gstvideooverlay.html)、[Qt RuntimeLoader](https://doc.qt.io/qt-6/qml-qtquick3d-assetutils-runtimeloader.html)。

## 2026-09-09 验证记录

- 已在真实 Jetson 显示三路视频；原有 Firefox 窗口按用户要求关闭，采集、建图和定位后端保持运行。
- 模拟后端的实际鼠标操作通过：进入任务、开始/停止录制、采集检查、加载已有 MP4、暂停后跳到约 5 秒、继续、返回三路实时。跳转后校验视频位置和暂停状态，客户端正常退出。
- 三模型载入成功，运动轨迹和姿态可见；头盔量化顶点转换后包围盒恢复为有限值。
- 修复了 nv3dsink 窗口句柄设置时机、原生子窗口销毁顺序、NVIDIA transform 会话未初始化导致的退出异常。回放使用每个 IDR 的 H.264 参数集、byte-stream 格式和解码后队列，解决本机暂停跳转阻塞。
- 初版反复扫描录制库时，编码/输出只有约 20–21 FPS；改成按需读取后，25 档基本达到目标，30 档编码约 29.1–29.3 FPS。最终每档预热 8 秒、采样约 20 秒，输出率使用各路自身采样时钟计算：

| 目标 | Insight3 A 输出 | Insight3 B 输出 | Insight9 输出 | 输出端丢弃 | 客户端 CPU |
| --- | --- | --- | --- | --- | --- |
| 25 FPS | 24.72 | 24.41 | 24.51 | 三路均 0 | 53% 单核 |
| 30 FPS | 28.96 | 28.56 | 29.39 | 三路均 0 | 78% 单核 |

CPU 来自客户端进程累计 ticks，不包含后端和 GPU；原始样本归档在本机 `~/workspaces/insight_capture_tests/native_dashboard_20260909/`。

以上是短时、未录制情况下的验证，不等于现场满负载录制或长时运行验收。视频输出计数也不是屏幕扫描次数；没有把不同时间采集的网页数据当作严格 A/B 性能结论。
