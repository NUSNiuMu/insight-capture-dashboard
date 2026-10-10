# EgoHalo 与 Native Qt 接入（候选配置）

新增 `jetson-nx-egohalo` 配置，设备组合为 Insight3 A、Insight3 B、EgoHalo。
使用同一份代码和原有 `./native_dashboard/run.sh`。原 `jetson-nx` 配置继续使用 Insight9。

## 数据与界面

- 一台设备对应一个位姿；EgoHalo 的四路视频按设备合并成 Qt 四宫格，总计六路视频、三个模型。
- EgoHalo 四路 `CompressedImage.format=h264` 是 1200×1600、约 30 Hz 的 Annex B access unit；不能按 JPEG 解码。预览直接转发 H.264 到 WebRTC，Qt 用硬件解码。预览 FPS 设置不改变源 H.264 编码码率、分辨率和帧率。
- 实时队列有界。丢帧、时间戳跳变或积压后等待带 SPS/PPS 的 IDR 恢复，不能任意抽取 P 帧。
- Insight3 保留现有原始红外输入与主机硬件编码路径；本次没有修改设备固件或改写其传输格式。
- 默认录制全部四路 EgoHalo 原始 H.264、对应 CameraInfo、IMU、原始 VIO、VIO 状态、静态外参，以及配置中的统一定位输出。录制保存原始数据；去畸变服务只供建图使用。
- 回放顺序解码 H.264 依赖帧，再选取同步显示帧。采用录制时间轴；开头裁到四路均可从 IDR 解码的位置。源时间戳断续记录在 manifest 的 `quality.codec_discontinuities`，解码器可能作错误隐藏，该样本不能作为完整性通过的证据。
- 多路 H.264 的 LeRobot/UMI 数据集导出尚未适配，会返回明确错误；本次范围是 Native 预览、录制、回放与建图接入。

## ROS 与建图

现场确认 EgoHalo 的 ROS domain 为 0，Insight3 为 20。配置驱动的 `device-domain-relay` 只转发源域与目标域不同的设备，原样保留 header 时间戳、CameraInfo 与 TF；不修改设备配置。EgoHalo 使用开机计时，不能把其时间戳直接当作 UTC。

`egohalo-stereo-rectifier` 消费前置双目 H.264、equidistant CameraInfo 和 TF，连续解码后输出 544×640、5 Hz、90° 水平视场的虚拟针孔双目，发布匹配的新内参与光学外参。共享源时钟选择同一曝光。该尺寸匹配当前 SuperPoint/SuperGlue 引擎。测得水平基线约 7 cm。

Mapper 输入话题与坐标系由配置覆盖。内部 `/insight9_sparse_map/*` 是保留的兼容接口名，不代表仍使用 Insight9 数据。Insight3 全局定位消费这张共享地图。EgoHalo 没有原 Insight9 RGB 立方标记输入，所以此配置关闭 cube-marker 校正，保留特征地图定位与采集前置检查。

## 部署与切回

此配置仍需现场运动验收；当前生产 profile 和运行客户端没有替换。以下命令仅在代码合入/镜像包含本变更、确认停止录制后使用：

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

验证在独立工作树、ROS domain 81、HTTP 18765 / WebRTC 18766 的临时容器完成，生产 domain 20 服务没有重启。跨域转发在模拟后台 shell 忽略 SIGINT 的条件下仍能正常退出（exit 0）。测试域额外转发了两台 Insight3；正式配置只跨域转发 EgoHalo。

- 真实 Native Qt 六路视频和三个位姿模型已显示；测试使用原始 VIO，仅验证数据与渲染，不能据此声称三个坐标系已对齐。
- 短录制正常退出并完成 MCAP，六路 376–387 帧、三路 VIO 1219–1265 条；从完整 IDR 开始生成 359 帧、约 11.97 秒合成回放。Qt 实际完成回放并显示三个模型。质量报告记录 3 次 H.264 源间断、180 个选帧重复、最大时间偏差 221.731 ms，三路位姿覆盖率均为 100%；完整性/同步质量为警告，未通过无丢帧验收。
- 真实校正图像有正常纹理；合成几何测试验证极线与三角化误差小于 1e-6。静态场景可产生双目匹配和三角化候选点，但确认地图点需要运动观测。
- 两台 Insight3 与 EgoHalo 的静态外参都能在隔离域收到。旧配置、录制、回放和导出等共 80 项检查通过。
- 未完成：现场运动建图、头部与双手统一定位精度/恢复验收、持续六路录制无丢帧验收、源到屏幕延迟测试。未切换生产配置。
