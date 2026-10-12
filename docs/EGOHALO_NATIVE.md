# EgoHalo 与 Native Qt 接入（候选配置）

新增 `jetson-nx-egohalo` 配置，设备组合为 Insight3 A、Insight3 B、EgoHalo。
使用同一份代码和原有 `./native_dashboard/run.sh`。原 `jetson-nx` 配置继续使用 Insight9。

## 数据与界面

- 一台设备对应一个位姿，总计六路视频、三个模型。Insight3 两路在左列，EgoHalo 在右侧两列组成四宫格。视频列按图像比例和可用高度收紧，完整等比例显示，省出的横向宽度交给 3D；2560×1440 全屏验证中视频区约 1245 px、3D 约 1274 px。缺少视频尺寸的回放保留原分栏宽度。
- EgoHalo 四路 `CompressedImage.format=h264` 是 1200×1600、约 30 Hz 的 Annex B access unit；不能按 JPEG 解码。本机 Qt 预览经 `/ws/native` 直接转发 H.264，远程网页仍用 WebRTC；Qt 用硬件解码；所有实时画面用 VIC 转为独立 I420 显示缓冲，高分辨率按比例缩到最长边 720（EgoHalo 为 540×720），避免显示端持有解码缓冲。原始录制仍为 1200×1600。预览 FPS 设置不改变源 H.264 编码码率、分辨率和帧率。
- 实时队列有界。真正丢参考帧、时间戳倒退或积压后等待带 SPS/PPS 的 IDR 恢复；对已识别的 progressive baseline/main I/P 码流，使用参考帧序号区分采样间隔变大和编码帧丢失，未知结构仍保守恢复，不能任意抽取 P 帧。
- Insight3 保留设备原始红外输入；本机 Qt 直接消费原始图像，网页保留主机硬件编码路径。没有修改设备固件或其传输格式。
- 默认录制全部四路 EgoHalo 原始 H.264、对应 CameraInfo、IMU、原始 VIO、VIO 状态、静态外参，以及配置中的统一定位输出。录制保存原始数据；去畸变服务只供建图使用。
- 回放顺序解码 H.264 依赖帧，再选取同步显示帧。采用录制时间轴；开头裁到四路均可从 IDR 解码的位置。源时间戳断续记录在 manifest 的 `quality.codec_discontinuities`，解码器可能作错误隐藏，该样本不能作为完整性通过的证据。
- 多路 H.264 的 LeRobot/UMI 数据集导出尚未适配，会返回明确错误；本次范围是 Native 预览、录制、回放与建图接入。

## ROS 与建图

现场确认 EgoHalo 的 ROS domain 为 0，Insight3 为 20。配置驱动的 `device-domain-relay` 只转发源域与目标域不同的设备，每个源域使用独立进程，直接转发序列化 CDR 字节，减少图像解包/重打包，原样保留 header 时间戳、CameraInfo 与 TF；不修改设备配置。EgoHalo 使用开机计时，不能把其时间戳直接当作 UTC。

`egohalo-stereo-rectifier` 消费四路 H.264、equidistant CameraInfo 和 TF，每个物理相机只解码一次，再组成前置双目、左侧重叠双目、右侧重叠双目。各组输出 544×640、5 Hz、90° 水平视场的虚拟针孔图像和对应内外参，以共享源时钟选择同一曝光。该尺寸匹配当前 SuperPoint/SuperGlue 引擎。四路共同参与地图观测和 Insight3 匹配，详细坐标、门限及验证见 [EGOHALO_MAPPING.md](EGOHALO_MAPPING.md)。

Mapper 输入话题与坐标系由配置覆盖。内部 `/insight9_sparse_map/*` 是保留的兼容接口名，不代表仍使用 Insight9 数据。Insight3 全局定位消费这张共享地图。EgoHalo 没有原 Insight9 RGB 立方标记输入，所以此配置关闭 cube-marker 校正，保留特征地图定位与采集前置检查。

### 2026-10-12：四路建图与统一位姿

现场运动后生成约 765 个确认地图点；A、B 在 30 秒状态采样中均持续通过真实几何匹配。三路全局位姿在独立 6 秒 ROS 观察中各收到 180 条消息，使用同一地图坐标。

排查模型相对位置时发现现场预览仍使用三套独立原始 VIO。现已切到配置模板规定的 `/insight_global/insight3_a/pose`、`/insight_global/insight3_b/pose`、`/insight9_sparse_map/pose`；默认 Qt 保留，用户确认 B 相对头部的位置正确。匹配成立不能替代模型安装外参的尺寸测量，绝对精度、全范围运动和长时间漂移仍需单独验收。此前原始 VIO 预览记录仅代表当时状态。

## 部署与切回

此配置仍需现场运动验收。运行时只保留一套后端和建图定位服务，禁止同时运行旧配置与测试配置；双套负载会显著增加延迟。以下命令在镜像包含本变更、确认停止录制后使用：

```bash
./scripts/select_device.sh jetson-nx-egohalo
docker compose --profile egohalo up -d device-domain-relay egohalo-stereo-rectifier
docker compose restart insight9-sparse-mapper insight3-global-localizer insight-dashboard
./native_dashboard/build.sh
./native_dashboard/run.sh
```

若 GPU 解码元素缺失，应重新构建镜像依赖，不能只在临时容器安装后视为已发布。开发和客户 compose 都有可选 `egohalo` profile；客户使用包含本变更的发布镜像及对应配置文件。

切回 Insight9 时同样先停止录制：

```bash
docker compose --profile egohalo stop egohalo-stereo-rectifier device-domain-relay
./scripts/select_device.sh jetson-nx
docker compose restart insight9-sparse-mapper insight3-global-localizer insight-dashboard
./native_dashboard/run.sh
```

## 2026-10-10 验证与边界

初期在独立工作树、domain 81、18765/18766 验证；发现双套服务竞争后，按用户要求停止旧配置，切为唯一的 domain 20、8765/8766 配置。Insight3 直接接收，只转发 domain 0 的 EgoHalo。原始设备配置未改动。当前现场预览采用原始 VIO；配置模板使用统一定位输出，后续需现场运动验收后启用该输出。

- 真实 Native Qt 六路视频和三个位姿模型已显示；测试使用原始 VIO，仅验证数据与渲染，不能据此声称三个坐标系已对齐。
- 短录制正常退出并完成 MCAP，六路 376–387 帧、三路 VIO 1219–1265 条；从完整 IDR 开始生成 359 帧、约 11.97 秒合成回放。Qt 实际完成回放并显示三个模型。质量报告记录 3 次 H.264 源间断、180 个选帧重复、最大时间偏差 221.731 ms，三路位姿覆盖率均为 100%；完整性/同步质量为警告，未通过无丢帧验收。
- 真实校正图像有正常纹理；合成几何测试验证极线与三角化误差小于 1e-6。静态场景可产生双目匹配和三角化候选点，但确认地图点需要运动观测。
- 两台 Insight3 与 EgoHalo 的静态外参都能在隔离域收到。旧配置、录制、回放、建图前置检查和导出等回归检查通过。建图检查按三个设备聚合六路图像，不再固定要求 Insight9。
- 未完成：现场运动建图、头部与双手统一定位精度/恢复验收、持续六路录制无丢帧验收、源到屏幕延迟测试。统一定位尚未作通过结论。

### 完整主机链路性能（单套配置）

定位到三类问题：双套服务占用 CPU、跨域/ROS 消息调度与重复 VIO 订阅、显示持有解码缓冲；另修复仅凭时间戳缺口误判 H.264 参考帧丢失的问题。EgoHalo 后端使用轻量 CDR 视图、逐流有序回调，配置提供单线程 ROS 调度；四路图像共用一条设备存活订阅。实时画面经 VIC 转为独立显示缓冲，回放保留正常 DPB。

25 秒六路预览与建图同时运行的采样：

| 指标 | 四路 EgoHalo 结果 |
| --- | --- |
| 实际显示 | 29.0–29.6 FPS |
| 主机源域收到同一帧 → 显示 sink 入口 | 中位 37.8–41.7 ms，P95 82.2–86.9 ms，最大 167.2 ms |
| 主机源域收到 → 后端 H.264 接收 | 中位 6.8–9.1 ms，P95 28.4–31.9 ms |
| 硬件解码阶段 | 中位 8.0–8.8 ms，P95 20.5–22.2 ms |
| 显示入口两帧间隔 | P95 68–74 ms，最大 169 ms |

Insight3 显示约 25 FPS，解码 P95 约 19 ms。三路轨迹约 27 Hz；EgoHalo 源域收到位姿 → Qt 应用模型变换，中位约 28.5 ms、P95 94.7 ms、最大 138.6 ms。没有要求现场运动，因此这只是传输和更新检查，不是统一定位精度验收。

这些指标使用同主机单调时钟按编码帧内容指纹、位姿原始时间戳配对，**不含相机曝光/内部编码、USB 到主机订阅前的耗时、显示器扫描输出**，不能标成端到端延迟。每路匹配约 730 帧；诊断本身带少量负载。真正缺参考帧时仍需等待 IDR，不能保证永不短暂停帧。

可在后端与 Qt 都设置 `INSIGHT_LATENCY_TRACE=1`，Qt 增加 `--diagnostics /tmp/native-latency.json`。默认关闭逐帧诊断，避免持续额外开销。`/api/cameras` 的 H.264 计数区分时间戳缺口、倒序、参考帧缺口、队列溢出和等待 IDR 丢弃。WebRTC 的 50 ms jitter 设置只约束该环节，不是整条链路时限。

验证：95 项回归检查；真实 CDR 与 ROS 序列化一致、所有截断样本被拒绝；1541 个录制 H.264 帧核对参考关系；原样跨域核对 241 条图像和 784 条 VIO 的 CDR 哈希一致。95 项检查和阶段时延结果不能替代长期无丢帧录制、运动定位与光学源到屏幕验收。

低延迟设置参考 [NVIDIA Jetson GStreamer 文档](https://docs.nvidia.com/jetson/archives/r36.3/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html)；抖动缓冲边界参考 [GStreamer rtpjitterbuffer](https://gstreamer.freedesktop.org/documentation/rtpmanager/rtpjitterbuffer.html)。

### Ego USB 离线影响 Insight3 的修复

2026-10-10 在 Ego USB 已拔掉、仅两台 Insight3 在线时，独立 Fast DDS/CycloneDDS 接收器都出现约 2 秒间断。相机源时间戳随后跳过多帧；USB 网卡入站包也暂停，主机 UDP 接收缓冲错误计数没有增加。抓包发现 Insight3 A 反复 ARP 查询已失效的主机地址 `169.254.10.2`，部分早于拔插创建的 DDS 进程仍绑定此地址。刷新唯一 ROS 服务栈后，两台 Insight3 恢复约 30 FPS，表明失效 DDS 地址是此次故障的重要触发条件。

候选 profile 新增 `ros_transport.exclude_host_addresses`：Dashboard、mapper、localizer 和 rectifier 在首个 ROS 初始化前生成 Fast DDS UDP 网卡白名单，排除 Ego 专用 USB 地址，保留当前其余接口、loopback 与共享内存传输。用户显式提供的 DDS XML 或其他 RMW 配置优先；旧 profile 不启用此行为。依据 [Fast DDS interface whitelist](https://fast-dds.docs.eprosima.com/en/2.x/fastdds/transport/whitelist.html) 和 [ROS 2 XML 配置说明](https://fast-dds.docs.eprosima.com/en/2.x/fastdds/ros2/ros2_configure.html)。

Ego 配置中的 `relay_host_address` 用于跨域桥接生命周期：源 USB 地址未分配时不创建 DDS 子进程；地址消失后停止子进程、撤回目标域 participant；重新出现后创建新子进程。该子进程同时接触源域与目标域，不使用上述本地域网卡排除规则。没有配置此字段的旧源域保留原行为。现场 Ego 实际重连仍待用户接入后验证。

相机 API 的输入/处理帧率改为按两秒墙钟窗口统计，停顿时间也进入分母；此前仅用首尾已收到帧的间隔，可能在“发一秒、停两秒”时仍显示约 30 FPS。实际显示帧率仍以 Qt 渲染计数为准。

部署防护后的约 90 秒 Native Qt 观察：

| 指标 | Insight3 A | Insight3 B |
| --- | --- | --- |
| 实际渲染 FPS | 29.86 | 29.92 |
| 输入 FPS 中位 / 最低 | 30.0 / 29.5 | 30.0 / 29.5 |
| 显示入口帧间隔 P95 / 最大 | 42.6 / 87.4 ms | 39.5 / 90.9 ms |
| 大于 100 ms 的显示入口帧间隔 | 0 | 0 |
| API 抽样的图像年龄最大 | 38.7 ms | 36.2 ms |
| API 抽样的 VIO 年龄最大 | 18.0 ms | 10.3 ms |
| 硬件解码 P95 | 5.6 ms | 6.4 ms |

观察窗口为约 89.91 秒、90 次状态采样，每路配对约 2680 个 Qt sink 时间点；诊断缓存中采样开始前的启动帧没有计入帧间隔。启动协商另有短间隔增加。这里的显示入口帧间隔与接收年龄仍不是光学端到端时延。

108 项回归检查通过；独立 ROS 测试域真实验证跨域桥接上线、离线撤回、再次上线恢复转发。现场两台 Insight3 恢复约 30 FPS；Ego 实物拔插恢复和完整六路更新版验证待后续接入。逐帧诊断在复测后关闭。

### 本机 Qt 直接预览与更新优化

优化前版本已推送为 `native-qt-stable-2026-10-10`（提交 `723ed28`，稳定版本地 Qt）。日常入口保持 `./native_dashboard/run.sh`；本机采用直接预览、远程网页继续用 WebRTC，并保留 `INSIGHT_NATIVE_TRANSPORT=webrtc` 对照方式。

Insight3 直接传主机已经收到的原始灰度图像，省掉 NVENC/WebRTC/NVDEC 中转；Ego 直接传设备原 H.264 到 NVDEC。录制先入队，原始保存与去畸变建图不变。任务状态异步读取/缓存，轨迹按变化更新并限制保留显示点数，离线卡片停止媒体连接。

两台真实 Insight3 90 秒采样：输出 29.99/30.00 FPS，显示入口帧间隔最大 51.2/43.6 ms；后端本机预览收到帧至 sink 入口 P95 8.9/8.3 ms，不是设备曝光至显示器的端到端延迟。保存的 Ego 原始 H.264 六路隔离 UI 验证与 MP4 回放操作通过；Ego 实物未连接，不能据此声称实际六路性能验收通过。详细记录见 [NATIVE_PERFORMANCE.md](NATIVE_PERFORMANCE.md)。
