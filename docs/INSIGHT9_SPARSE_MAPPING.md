# Insight9 在线稀疏建图验证

该验证节点使用 Insight9 校正后的左右红外图、100 Hz VIO 和 Magic Leap 官方
SuperPoint/SuperGlue，在当前会话内建立稀疏地图，并发布 RViz 点云与相机轨迹。

## 当前边界

- 官方代码及权重固定到提交
  `ddcf11f42e7e0732a0c4607648f9448ea8d73590`。商用/再分发用途已与 Magic Leap
  确认，v2.0.0 起随客户发布镜像一起分发。
- `Dockerfile.superglue-validation` 使用多阶段构建：固定 digest 的 NVIDIA
  PyTorch 镜像只负责把官方模型导出为 ONNX；固定 digest 的 NVIDIA TensorRT
  `25.04-py3-igpu` 只作为运行库来源，最终镜像基于 Ubuntu 24.04，仅复制建引擎
  和推理需要的 TensorRT/CUDA 动态库。
- 运行镜像不包含 PyTorch、PyTorch wheel、ONNX 导出器或 PyTorch 回退路径。
  也不包含 CUDA 开发工具、HPC-X/NCCL、TensorRT samples/headers、静态库或
  Lean/Dispatch 变体。推理时只使用 TensorRT、CUDA runtime 和 NumPy；CUDA
  显存与 stream 通过 CUDA C API 直接管理。
- SuperPoint 的卷积、softmax、NMS、边界过滤、Top-K 和描述子采样全部位于
  FP16 TensorRT 引擎内；数值敏感的 SuperGlue/Sinkhorn 使用 FP32。
- `/insight9_a/camera/hand_keypoints` 使用彩色相机坐标系，不能直接作为红外图像
  遮罩。当前通过至少三个关键帧的世界坐标一致性过滤移动人手和物体。

## 数据链

```text
左右 mono8 图像
  -> 20 ms 近时同步
  -> 官方 SuperPoint + SuperGlue
  -> 极线、视差、深度和重投影过滤
  -> 双目三角化
  -> 100 Hz VIO 插值到图像时间戳
  -> 历史 3D 地标 PnP 回环检测
  -> T_map_odom * T_odom_imu * T_imu_left
  -> 世界坐标体素确认
  -> PointCloud2 + Path + TF
```

稳定地图点必须在三个不同关键帧落入同一个 4 cm 体素。候选点超过 12 个关键帧
未再次观测会被删除，避免单帧动态物体永久进入地图。

Insight9 每两个关键帧尝试一次自身回环，并从候选地图中排除最近 30 个关键帧
内首次建立的地标，避免把连续跟踪误判成回环。当前左目特征与历史三维地标通过
描述子互检和比率测试后，由 PnP-RANSAC 做几何验证；默认要求至少 15 个内点、
55% 内点率、覆盖 5 个 4x4 图像网格，并连续三次得到一致校正才接受。
接受后的 `T_map_odom` 经误差状态 EKF 和 0.75 秒时间常数平滑应用；设备原始
VIO 不被写回，新地标、Insight9 全局 Pose、Path 和 TF 使用校正后的坐标。

## 构建和启动官方 GPU 服务

开发机首次构建：

```bash
docker compose build superglue-inference
```

建图已接入开发版 dashboard，也已纳入 `scripts/build_release.sh` 的客户发布打包。
本地开发时，正常启动 dashboard 即会通过依赖关系同时启动
TensorRT 推理、Insight9 mapper 和 Insight3 localizer：

```bash
docker compose up -d --wait insight-dashboard
```

打开 `http://<设备地址>:8765/3d` 可直接查看稀疏建图状态，以及 Insight9、
Insight3 A、Insight3 B 三条全局轨迹。页面会显示三路在线状态、Insight9
关键帧和最近一次晋升的地图点数量；点击 **New map** 会清空当前地图、三条轨迹
和两个 Insight3 已确认的全局校正，从当前相机位姿开始新会话。
建图在线时网页只显示这三条全局轨迹，不再叠加原有的三条局部 VIO 轨迹；
三个模型的位置和朝向也直接使用对应全局 Pose。建图离线时不会回退到局部
VIO，避免模型在两套坐标源之间跳变。

网页不渲染稀疏特征点云，只显示点数统计和三条全局轨迹。模型位姿使用独立的
高频全局 Pose 话题，唯一的 dashboard WebSocket 以 50 Hz 发送最新位姿，
前端用同一份 Pose 增量绘制轨迹。完整 ROS Path 限制为 200 点并以 5 Hz
发布，仅供 RViz 和显式调试选择，避免反复序列化整条历史；网页和默认录制
只使用 50 Hz Pose，也不再建立第二条 mapping WebSocket。建图状态通过
500 ms 的轻量 REST 轮询显示。

新录制默认保存三路全局 Pose；回放时三路全局 Pose 经 `/bagplay/...`
remap 后继续驱动同一套模型和轨迹。旧 rosbag 如果没有这些全局话题，将不显示
轨迹或模型位置，不会回退到旧 VIO。原 AprilTag 在线对齐的订阅、定时器、
Web API、前端控制和 WebSocket payload 已停用，建图重定位是唯一在线校准源。

验证镜像包含：

- NVIDIA Jetson TensorRT `25.04-py3-igpu` 运行时；
- 构建阶段预导出的 `superpoint.onnx` 和 `superglue.onnx`；
- 官方 SuperPoint/SuperGlue 提交 `ddcf11f...`；
- `superpoint_v1.pth`；
- `superglue_indoor.pth`；
- `superglue_outdoor.pth`。

构建阶段会校验三份权重的 SHA-256；任一内容不一致即失败。该镜像名为
`insight-superglue-validation:25.04`（沿用历史命名，未随发布状态改名），
由 `scripts/build_release.sh` 一并构建并打进客户镜像 tar。

ONNX 在镜像构建阶段一次性导出。首次启动只在当前 Jetson 上编译
`superpoint_fp16.plan` 和 `superglue_fp32.plan`，不需要也不会加载
PyTorch。引擎绑定 TensorRT、CUDA、GPU compute capability、输入规格和模型
参数，校验结果及 SHA-256 写入 manifest；不匹配时自动重建。生成物保存在
Docker volume `superglue-engines`，后续重启直接加载，也不把设备专用 plan
提交到 Git。

dashboard 和映射容器使用 host IPC。这是 Fast DDS 跨容器及宿主机 RViz
传输所需：只有 host network 时可以发现话题，但共享内存数据端点在另一个
IPC 命名空间中不可达。

查看启动状态：

```bash
docker compose ps
docker compose logs -f \
  superglue-inference insight9-sparse-mapper
```

首次构建 RViz 验证镜像：

```bash
docker compose --profile mapping-validation build insight9-mapping-rviz
```

RViz 仅保留为调试工具。每次清空旧地图并打开 RViz：

```bash
scripts/run_mapping_validation_rviz.sh
```

脚本保持在前台，关闭 RViz 后自动收回临时 X11 授权。它会先重建 mapper 和
localizer，确保不继续显示上一会话的内存地图；关闭 RViz 后三个核心服务继续
运行，网页仍可查看和新建地图。
当前 RViz 验证配置只显示稀疏确认地图和三条全局轨迹，不启动稠密 mapper。

网页接口：

- `GET /api/mapping`：当前地图点数和三路状态快照。
- `GET /ws`：50 Hz 三路全局位姿流，同时驱动模型和网页轨迹。
- `POST /api/mapping/reset`：同时重置 mapper 与 localizer。
- ROS service `/insight9_sparse_map/reset`：清空 Insight9 会话地图。
- ROS service `/insight_global/reset`：清空两个 Insight3 的校正和全局轨迹。

主要输出：

- `/insight9_sparse_map/points`：经过多关键帧确认的稀疏地图。
- `/insight9_sparse_map/features`：确认地标的三维位置和 256 维 SuperPoint 描述子。
- `/insight9_sparse_map/pose`：50 Hz 最新全局位姿。
- `/insight9_sparse_map/path`：5 Hz 发布、最多 200 点的调试全局轨迹。
- `/insight9_sparse_map/status`：匹配数、三角化数、稳定点数、回环候选/拒绝
  诊断、累计接受次数和处理耗时 JSON。
- TF `insight9_map -> insight9_mapping_camera_center`：位于左右目光心中点，
  姿态沿用左目；使用独立命名避免与设备 TF 多父冲突。

查看状态：

```bash
ros2 topic echo /insight9_sparse_map/status
```

需要完全停止建图服务时：

```bash
docker compose stop insight3-global-localizer insight9-sparse-mapper superglue-inference
```

用该特征地图定位两路 Insight3 的方法见
[INSIGHT3_GLOBAL_LOCALIZATION.md](INSIGHT3_GLOBAL_LOCALIZATION.md)。

## 第一轮验收

- 左右图时间差不超过 20 ms。
- 稳定场景每关键帧有可重复的几何有效点。
- 点云随相机移动保持在世界坐标中，不跟随相机漂移。
- 人手或移动物体的单帧点不进入稳定点云。
- VIO 时间戳回退时自动清空当前会话地图和轨迹。
- 返回较早区域后 `loop_confirmation_progress` 连续达到 3，
  `loop_closures` 增加，Insight9 全局 Pose 被拉回历史地图。
- 无回环和重复纹理数据中不接受错误回环。
- 记录 `stereo_matches`、`triangulated`、`confirmed` 和
  `inference_and_geometry_ms`，再判断 5 Hz 目标是否成立。

## 2026-07-27 实机结果

Jetson Orin NX、544×640 双目红外输入、1024 个最大关键点、官方 indoor
权重下：

- 纯运行镜像内 `import torch` 不可用，IPC health 返回后端
  `tensorrt-cuda`、TensorRT 10.9.0.34、CUDA 12.9 和 SM 8.7；
- Docker 解包占用由 PyTorch 开发镜像的约 17.4 GB 降到 9.48 GB，其中
  NVIDIA TensorRT 基础层为 8.88 GB；
- 将完整 SuperPoint 后处理移入引擎后，预热特征提取平均 21.69 ms，双图
  SuperPoint + SuperGlue 完整匹配平均 68.81 ms；
- 固定纹理精度对照中，PyTorch 与纯 TensorRT 都检测到 209 个关键点，
  关键点集合 Jaccard 为 0.8914；匹配数分别为 193 和 187，匹配集合
  Jaccard 为 0.8537；
- TensorRT 10.9 引擎缓存校验为 SM 8.7，SuperPoint FP16 纯引擎平均
  10.68 ms；
- SuperGlue 256/512 点 FP16 纯引擎分别为 47.25/60.05 ms，但 FP16
  Sinkhorn 在精度对照中明显漏配，因此没有采用；
- 最终使用 SuperPoint FP16 + SuperGlue FP32。在固定平移纹理对照中，
  PyTorch/TensorRT 都得到 21 条匹配，19 条完全相同；
- 实时帧样本为 202–211 个 SuperGlue 匹配、152–157 个有效三角化点；
- TensorRT 实时链路样本得到 299 个双目匹配、230 个三角化点，后端推理
  286.16 ms，含几何处理 318.80 ms；
- 强制静止画面连续关键帧后，170 个关键帧形成 976 个已确认地图点；
- 发布点云宽度达到 982，路径保持在配置的 200 点上限；
- dashboard 容器已实际收到 mapper 容器发布的完整状态消息。
- localizer 直接订阅两路 20 Hz 原始图时，Insight3 B 实时图像曾下降到
  13–14 Hz，六核 CPU 在录制期间达到 75–90%/核；改为复用 dashboard reader
  并以 2 Hz 中继定位图后，A/B 连续实测均恢复为 20 Hz。
- 完整 200 点 Path 改为 5 Hz 调试输出，默认录制只保留 50 Hz Pose；三个
  全局 Pose 合并进对应相机 recorder，录制 part 数由 9 降为 7。
- 在 3D/WebRTC 同时运行的 30 秒压力录制中，三路图像 live audit 为
  602/602/904 帧，header missing 和 writer drop 均为 0；为 400 Hz IMU
  配置 1000 深度 rosbag QoS 后，三路 IMU 与所有受检话题也均为 0% 丢失。

结论：在线进程已是纯 TensorRT/CUDA runtime，PyTorch 只存在于镜像构建阶段，
且没有为了速度接受 SuperGlue FP16 精度回归。当前共享 GPU 负载下端到端吞吐
仍未达到稳定 5 Hz；下一步性能优化应针对 SuperGlue 的受约束混合精度和并发
GPU 负载，不能直接把整个 Sinkhorn 降为 FP16。世界坐标静态性、动态物体过滤
和长时漂移仍需移动相机采集后验收。
