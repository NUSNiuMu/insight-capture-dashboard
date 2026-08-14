# 头部在线稀疏建图与手部相机全局重定位

在线栈使用配置头部相机校正后的左右目、100 Hz VIO 和 Magic Leap 官方
SuperPoint/SuperGlue，在当前会话内建立稀疏地图；配置的左右手相机随后定位到同一
地图并持续发布统一世界系 Pose。当前 Jetson NX 的三路 Insight7 左右目均为 NV12
彩色流（固件沿用 `infra1/infra2` topic 名）；特征提取只取 Y 平面。v2.0.0 起该栈
进入客户发布，RViz/稠密地图仍只作内部验证。

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
左右 mono8/NV12 图像（特征使用亮度平面）
  -> 20 ms 近时同步
  -> 官方 SuperPoint + SuperGlue
  -> 极线、视差、深度和重投影过滤
  -> 双目三角化
  -> 100 Hz VIO 插值到图像时间戳
  -> 历史 3D 地标 PnP 回环检测
  -> 关键帧 VIO 边 + 全局回环边
  -> 鲁棒 SE(3) 位姿图优化
  -> 优化位姿下重融合历史双目观测
  -> T_map_odom * T_odom_imu * T_imu_left
  -> 世界坐标体素确认或重建
  -> features PointCloud2 + Pose + TF
  -> 可选的调试 points PointCloud2 + Path
```

稳定地图点必须在三个不同关键帧落入同一个 4 cm 体素。候选点超过 12 个关键帧
未再次观测会被删除，避免单帧动态物体永久进入地图。

Insight9 每两个关键帧尝试一次自身回环，并从候选地图中排除最近 30 个关键帧
内首次建立的地标，避免把连续跟踪误判成回环。当前左目特征与历史三维地标通过
描述子互检和比率测试后，由 PnP-RANSAC 做几何验证；默认要求至少 15 个内点、
55% 内点率、覆盖 5 个 4x4 图像网格，并连续三次得到一致校正才接受。
接受回环后不再只更新一个统一的 `T_map_odom`：当前关键帧的 PnP 全局位姿会
形成从固定首帧到当前帧的回环边，与相邻关键帧的 VIO 相对位姿边一起进入鲁棒
SE(3) 位姿图。优化结果沿整段轨迹分配校正，并用所有保留的双目局部观测重建
地标地图，因此回环前已经写入的旧地标也会移动到优化后的坐标。

最新 Pose 和 TF 仍通过误差状态 EKF 与 0.75 秒时间常数平滑，避免回环造成单帧
跳变；调试 Path 按关键帧时间插值各段图优化校正，能显示被修正后的历史轨迹。
设备原始 VIO 不被写回。图优化至少间隔 5 秒运行，但首个回环、达到 10 cm 或
5 度差异的回环会立即求解；限流窗口内加入的边会由后续关键帧自动补做，不能
永久停留在 pending 状态。

每个会话最多保留 600 个图节点。局部三维观测使用 FP32，描述子与分数以 FP16
保留，达到上限后冻结已建图图结构并继续使用当前全局校正，避免长会话无界占用
内存。历史 Path 仍保留已经求得的分段图校正。本阶段是“位姿图优化 + 地标
重融合”，不是联合优化像素重投影误差的完整 Bundle Adjustment；后续若需要把
标定残差和每个地标观测一并优化，还需增加显式的 2D 观测边和局部 BA 窗口。

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

打开 `http://<设备地址>:8765/3d` 可直接查看稀疏建图状态，以及头部、左手、右手
三条全局轨迹。页面会显示三路在线状态、头部
关键帧和最近一次晋升的地图点数量；点击 **New map** 会清空当前地图、三条轨迹
和两个手部相机已确认的全局校正，从当前相机位姿开始新会话。
建图在线时网页只显示这三条全局轨迹，不再叠加原有的三条局部 VIO 轨迹；
三个模型的位置和朝向也直接使用对应全局 Pose。建图离线时不会回退到局部
VIO，避免模型在两套坐标源之间跳变。

网页不渲染稀疏特征点云，只显示点数统计和三条全局轨迹。模型位姿使用独立的
高频全局 Pose 话题，唯一的 dashboard WebSocket 以 30 Hz 发送最新位姿，
前端用同一份 Pose 增量绘制轨迹。完整 ROS Path 限制为 200 点，仅供 RViz 和
显式调试选择；这些 Path 和稀疏点云默认不创建 publisher，显式传入
`--publish-debug-topics` 后才以 2 Hz/按变化发布。网页和默认录制只使用 30 Hz
Pose，也不再建立第二条 mapping WebSocket。建图状态通过 500 ms 的轻量 REST
轮询显示。

新录制默认保存三路全局 Pose；回放时三路全局 Pose 经 `/bagplay/...`
remap 后继续驱动同一套模型和轨迹。旧 rosbag 如果没有这些全局话题，将不显示
轨迹或模型位置，不会回退到旧 VIO。原 AprilTag 在线对齐的订阅、定时器、
Web API、前端控制和 WebSocket payload 已停用，建图重定位是唯一在线校准源。

## 手部相机全局重定位

Dashboard 按 `global_localization_image_stream` 订阅手部相机左目，并以 2 Hz 中继到
`/insight_mapping/<name>/infra1/image_rect_raw`；若网页显示同一流则复用 reader，否则
由 Dashboard 建立独立 reader，localizer 自身不再重复订阅原始全速图像。查询特征与
头部相机的 3D 描述子地图匹配，通过 PnP-RANSAC 与连续三帧
共识后得到 `T_map_odom`，再以 30 Hz 原始 VIO 外推全局 Pose。

- 首次定位直接初始化；小于 `0.15 m / 10°` 的修正由误差状态 EKF 平滑吸收；达到
  任一阈值的可靠重定位立即跳回并从当前位置重建 Path。
- 暂时看不到已建图区域时保留最后校正并进入 `vio_only`，不会隐藏模型；再次匹配
  地图后继续校准累积漂移。
- 每路 Insight3 在进入全局重定位前会先检查原生 VIO 的瞬时坐标重置。相机原生
  状态为 `TRACKING_STATIC` 时，恒速预测出现 `3 cm / 5°` 创新会在当帧对新 VIO
  分段应用刚体校正；每个 VIO 输入仍同步产生一个 Pose，不为确认缓存或中断实时
  输出。普通 `TRACKING` 运动期间只记录候选而不自动改坐标，因为仅凭位姿流无法
  区分真实快速运动和坐标重置。超过 50 ms 的跟踪空洞同样不会自动拼接；缓慢累计
  漂移仍由地图重定位校正。
- Insight3 图像底部夹爪 mask 默认为 `0`（关闭），可在 Settings 按现场遮挡比例热
  更新；不能假设固定 20%。
- Jetson NX profile 发布 `insight3_a_global_camera_center -> right_tcp` 与
  `insight3_b_global_camera_center -> left_tcp` 静态 TF；未标定 profile 不发布占位变换。
- `/insight_global/<name>/status` 提供 `tracking_mode`、`correction_mode`、PnP/共识、
  hard relocalization、当前 mask 和 `vio_continuity` 等诊断字段。后者包含已确认的
  拼接次数、受状态门抑制的候选、跟踪空洞、累计局部校正、输入/输出样本计数以及
  最近一次跳变的时间和幅度；正常实时链路的 `input_samples` 和 `output_samples` 必须相等。

验证镜像包含：

- NVIDIA Jetson TensorRT `25.04-py3-igpu` 运行时；
- 构建阶段预导出的 `superpoint.onnx` 和 `superglue.onnx`；
- 官方 SuperPoint/SuperGlue 提交 `ddcf11f...`；
- `superpoint_v1.pth`；
- `superglue_indoor.pth`；
- `superglue_outdoor.pth`。

构建阶段会校验三份权重的 SHA-256；任一内容不一致即失败。该镜像名为
`insight-superglue-validation:25.04`（沿用历史命名，未随发布状态改名），
由 `scripts/build_release.sh` 一并构建并单独保存为首次安装依赖 tar；Dashboard
日常升级包不再重复携带该固定镜像。

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
- `GET /ws`：30 Hz 三路全局位姿流，同时驱动模型和网页轨迹。
- `POST /api/mapping/reset`：同时重置 mapper 与 localizer。
- ROS service `/insight9_sparse_map/reset`：清空 Insight9 会话地图。
- ROS service `/insight_global/reset`：清空两个 Insight3 的校正和全局轨迹。

默认输出：

- `/insight9_sparse_map/features`：确认地标的三维位置和 256 维 SuperPoint 描述子。
- `/insight9_sparse_map/pose`：30 Hz 最新全局位姿。
- `/insight9_sparse_map/status`：匹配数、三角化数、稳定点数、回环候选/拒绝
  诊断、图节点/边/回环边数量、pending 状态、图优化前后代价、最大位姿修正、
  地图重建耗时、观测内存和累计接受次数 JSON。
- TF `insight9_map -> insight9_mapping_camera_center`：位于左右目光心中点，
  姿态沿用左目；使用独立命名避免与设备 TF 多父冲突。

以下 RViz 调试输出默认关闭；同时为 mapper 和 localizer 传入
`--publish-debug-topics` 后才创建 publisher：

- `/insight9_sparse_map/points`：经过多关键帧确认的稀疏地图。
- `/insight9_sparse_map/path`：2 Hz、最多 200 点的 Insight9 调试轨迹。
- `/insight_global/insight3_a/path`、`/insight_global/insight3_b/path`：2 Hz、
  最多 200 点的 Insight3 调试轨迹。

关闭调试输出不影响 30 Hz Pose、5 Hz TF、网页轨迹、状态或默认录制。

查看状态：

```bash
ros2 topic echo /insight9_sparse_map/status
```

需要完全停止建图服务时：

```bash
docker compose stop insight3-global-localizer insight9-sparse-mapper superglue-inference
```

## 第一轮验收

- 左右图时间差不超过 20 ms。
- 稳定场景每关键帧有可重复的几何有效点。
- 点云随相机移动保持在世界坐标中，不跟随相机漂移。
- 人手或移动物体的单帧点不进入稳定点云。
- VIO 时间戳回退时自动清空当前会话地图和轨迹。
- 返回较早区域后 `loop_confirmation_progress` 连续达到 3，
  `loop_closures` 与 `pose_graph_loop_edges` 增加，图优化代价下降，Insight9
  全局 Pose 被拉回历史地图。
- 回环优化后 `pose_graph_optimizations` 增加，`map_rebuild_ms` 非零；旧地标、
  历史 Path 与当前 Pose 处于同一优化后坐标，不只修正后续输出。
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
- 完整 200 点 Path 后续降为 2 Hz 并改成默认关闭的调试输出；当前默认录制只保留
  30 Hz Pose；三个全局 Pose 合并进对应相机 recorder，录制 part 数由 9 降为 7。
- 在 3D/WebRTC 同时运行的 30 秒压力录制中，三路图像 live audit 为
  602/602/904 帧，header missing 和 writer drop 均为 0；为 400 Hz IMU
  配置 1000 深度 rosbag QoS 后，三路 IMU 与所有受检话题也均为 0% 丢失。

结论：在线进程已是纯 TensorRT/CUDA runtime，PyTorch 只存在于镜像构建阶段，
且没有为了速度接受 SuperGlue FP16 精度回归。当前共享 GPU 负载下端到端吞吐
仍未达到稳定 5 Hz；下一步性能优化应针对 SuperGlue 的受约束混合精度和并发
GPU 负载，不能直接把整个 Sinkhorn 降为 FP16。世界坐标静态性、动态物体过滤
和长时漂移仍需移动相机采集后验收。

## 2026-08-05 位姿图验证

位姿图与地标重融合已接入在线 mapper，并在 Jetson Orin NX 当前运行容器中完成
数值和真实输入链路验证：

- 101 节点、末端 10 cm 平移漂移的合成轨迹，回环后末端为 10.00385 m，
  求解耗时 194.0 ms；
- 61 节点同时含平移和旋转漂移的轨迹，回环后旋转残差为 0.001203 rad，
  求解耗时 164.2 ms；
- 在正确回环外再加入一个相差约 3 m、2 m 和 0.5 rad 的错误回环，Cauchy
  鲁棒核仍将末端保持在 `[10.0132, 0.0136, 0]` m；
- 600 节点上限轨迹的单次求解耗时 1.80 秒，末端残差 3.98 mm；
- 170 个关键帧、每帧 150 个局部观测的地图重融合耗时 703.1 ms，保留观测
  占用 13.43 MB；
- 重启在线 mapper/localizer 后，真实 544×640 双目首关键帧得到 251 条匹配、
  173 个有效三角化点，状态发布图节点、边、pending 和内存字段，服务无新异常。

这些结果验证了优化器收敛、错误边抑制、资源上限和在线接线，不代表已经完成
真实空间的厘米级精度验收。最终精度仍需移动相机完成长轨迹闭环，并用独立的
固定控制点或全站仪/标定板测量建图形变与 Insight3 重定位误差。
