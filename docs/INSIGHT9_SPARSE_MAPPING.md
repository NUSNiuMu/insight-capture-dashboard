# Insight9 在线稀疏建图与 Insight3 全局重定位

在线栈使用 Insight9 校正后的左右红外图、100 Hz VIO 和 Magic Leap 官方
SuperPoint/SuperGlue，在当前会话内建立稀疏地图；两路 Insight3 随后定位到同一
地图并持续发布统一世界系 Pose。v2.0.0 起该栈进入客户发布，RViz/稠密地图仍只作
内部验证。

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
  -> 双目匹配的亚像素 LK 细化与前后向检查
  -> 极线、视差、深度和重投影过滤
  -> 双目三角化
  -> 100 Hz VIO 插值到图像时间戳
  -> 历史 3D 地标 PnP 回环检测
  -> 关键帧 VIO 边 + 全局回环边
  -> 鲁棒 SE(3) 位姿图优化
  -> 优化位姿下重融合历史双目观测
  -> 后台滑窗双目 BA 联合优化关键帧 Pose 与重复 3D 地标
  -> T_map_odom * T_odom_imu * T_imu_left
  -> 描述子地标关联、确认或重建
  -> features PointCloud2 + Pose + TF
  -> 仅发布 BA 已优化共享地标的 XYZ / 描述子
  -> Insight3 特征匹配 + PnP
  -> 可选的调试 points PointCloud2 + Path
```

关键帧按时间限频采样，默认最高 2 Hz，不依赖相机平移或旋转。静止时同样收集双目关键帧。
稳定地图点必须在三个不同关键帧重复观测到同一特征。关联时以 4 cm 体素查找邻域：
在相邻体素中查找距离不超过 4 cm、归一化描述子余弦相似度至少 0.80 的地标，
另需通过 0.80 描述子距离比率测试，避免把重复纹理关联到不确定的地标。
同一体素可以保留不同特征。单地标在同一关键帧最多更新一次，候选超过 12 个
关键帧未再次观测会被删除。位置按独立观测平均，描述子保留等权观测和；
候选与确认地标合计受资源上限约束。发布用于定位的描述子地图时保留所有已确认的
独立特征；体素仅用于加速关联，图像空间覆盖由 PnP 几何门限检查。
PnP 的 RANSAC 内点使用 1 px Huber 损失和解析雅可比细化，最多 20 次函数求值，
不收敛时拒绝本次候选。对照验证见
[地标关联精度验证](MAPPING_PRECISION_VALIDATION.md)。

Insight9 每两个关键帧匹配一次与腕部共用的 BA 地标，不因录制参考帧切换定位来源。
当前左目特征经描述子互检和比率测试、PnP-RANSAC 与鲁棒细化后，默认要求至少
15 个内点、55% 内点率并覆盖 5 个 4x4 图像网格；当前候选须属于最近五次尝试中
至少三个相互一致候选组成的簇。使用当前观测，旧修正不会替代当前解。

确认的视觉观测先进入实时 EKF，再作为全局边参与历史位姿图优化。图优化和后台
BA 修正历史轨迹与地标，不再把历史图修正写回实时 EKF。设备原始 VIO 不被写回。
图优化至少间隔 5 秒运行，但首个回环、达到 10 cm 或 5 度差异的回环会立即求解；
延后求解在当前视觉更新之后执行，不跳过当前观测。

最新 Pose 和 TF 由单一误差状态 EKF 输出，没有第二层低通平滑。PnP 投影雅可比
给出完整 6×6 观测协方差，保留位置与旋转相关性；EKF 在实际相机位姿处融合，
避免里程计原点的旋转杠杆放大位置偏差。退化协方差直接拒绝，不采用替代噪声路径。
调试 Path 仍按关键帧时间插值图优化后的历史轨迹。

每个会话最多保留 600 个图节点。局部三维观测使用 FP32，描述子与分数以 FP16
保留，达到上限后冻结已建图图结构并继续使用当前全局校正，避免长会话无界占用
内存。历史 Path 仍保留已经求得的分段图校正。

位姿图之外还启用有界局部 Bundle Adjustment：每个关键帧保留通过双目几何门限的
左右 2D 像素、局部 3D 点和 SuperPoint 描述子；后台线程在最近 12 个关键帧中，
通过描述子互检和 12 cm 世界坐标门限建立重复地标，固定窗口首帧，并以左右目
重投影误差和相邻 Pose 先验联合优化关键帧 Pose 与统一 3D 地标。默认每 10 个
关键帧调度一次，首次建图每 4 帧调度；少于 20 个重复地标不求解，每帧最多取
200 个高置信点、最多优化 250 个地标，每轮最多 10 次函数求值。
求解快照带会话代次和 Pose 修订号：VIO reset 或新的回环图优化会使旧结果失效；
求解期间新增的尾段关键帧会继承窗口末端的刚体修正，避免轨迹断层。
联合求解使用 Soft-L1 鲁棒损失和固定变量尺度；先鲁棒优化，再剔除重投影误差或
三维修正超限的轨迹，并对保留观测再次优化，避免错误轨迹影响最终位姿。
前后误差使用完全相同的保留观测计算；已在像素质量门限内的静止窗口允许无劣化
解，不要求凭空产生误差降幅。Pose 修正仍受平移 10 cm、旋转 5° 门限约束。
这里只发布通过多帧优化的三维点，不优化相机内参、双目外参或时间偏移。

共享地标以多帧度量双目点的均值初始化，再联合优化左右像素残差和 VIO 先验，
不把固定错误 VIO 位姿下的多视图 DLT 深度直接作为优化初值。DLT 仍用于排除
负深度、无穷远和最大有效视差角小于 0.5° 的退化轨迹。
每条保留地标相对原均值的修正不超过 20 cm，最终轨迹重投影 RMSE 不超过 2 px。
验证和重建采集方法见 [重建图前优化与数据规范](MAPPING_REBUILD_VALIDATION.md)。

## 构建和启动官方 GPU 服务

开发机首次构建：

```bash
docker compose build superglue-inference
```

建图已接入开发版 dashboard，也已纳入 `deploy/build_release.sh` 的客户发布打包。
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

## Insight3 全局重定位

Dashboard 复用两路 Insight3 现有图像 reader，以 2 Hz 中继到
`/insight_mapping/<name>/infra1/image_rect_raw`；localizer 不再增加全速图像 DDS
reader。Insight9 的双目匹配先细化右目像素，左目坐标与 SuperPoint 描述子保持
对应；失败、位移过大或前后向不一致的匹配直接丢弃。LK 使用初始匹配位置，
方法接口见 [OpenCV 光流文档](https://docs.opencv.org/4.x/dc/d6b/group__video__track.html)。

在线地标集合只接收通过 BA 整窗门限的跨帧共享点；未参与优化的单帧点保留用于
建图观测和诊断，不进入定位地图。Insight3 只匹配这份优化地标集合并执行 PnP，
不再订阅单参考帧进行另一条定位尝试。地图尚未成熟时状态明确等待；已有定位后
缺少匹配时由原始 VIO 延续轨迹。BA 整窗验收门限保持不变。

参考关键帧图像和 UV/XYZ 话题继续保存为诊断数据，不作为腕部主定位输入。
mapper 的 OpenBLAS / OpenCV 工作线程限制为 1，避免小矩阵计算争用 CPU。

PnP 默认以 2 Hz 尝试定位，最近五次尝试中至少三个候选相互一致且包含当前候选
才确认当前观测；两帧旧修正不会否决新三帧多数解，确认后保留滚动窗口。单次
图像模糊或遮挡不会清空已有进度，也不会重新应用历史观测。确认后得到
`T_map_odom`，再以原始 VIO 外推并以 30 Hz 发布全局 Pose。

- 首次定位直接初始化；小于 `0.15 m / 10°` 的修正由误差状态 EKF 平滑吸收；达到
  任一阈值的可靠重定位立即跳回并从当前位置重建 Path。
- 暂时看不到已建图区域时保留最后校正并进入 `vio_only`，不会隐藏模型；再次匹配
  地图后继续校准累积漂移。
- 原生 VIO 直接用于外推，不施加静止保持，也不根据 `TRACKING_STATIC` 自动拼接
  或改写位姿跳变。VIO 时间戳回退会清除全局定位，等待新的可靠视觉观测；累计
  漂移由地图匹配校正。设备静止状态只用于诊断。
- Insight3 图像底部夹爪 mask 当前 profile 默认为 `0.2`，可在 Settings 按现场遮挡比例热
  更新；不能假设固定 20%。
- Jetson NX profile 发布 `insight3_a_global_camera_center -> right_tcp` 与
  `insight3_b_global_camera_center -> left_tcp` 静态 TF；未标定 profile 不发布占位变换。
- `/insight_global/<name>/status` 提供 `tracking_mode`、`correction_mode`、PnP/共识、
  hard relocalization、当前 mask、`device_tracking_status`、完整观测协方差和位置/
  旋转创新等诊断字段。

验证镜像包含：

- NVIDIA Jetson TensorRT `25.04-py3-igpu` 运行时；
- 构建阶段预导出的 `superpoint.onnx` 和 `superglue.onnx`；
- 官方 SuperPoint/SuperGlue 提交 `ddcf11f...`；
- `superpoint_v1.pth`；
- `superglue_indoor.pth`；
- `superglue_outdoor.pth`。

构建阶段会校验三份权重的 SHA-256；任一内容不一致即失败。该镜像名为
`insight-superglue-validation:25.04`（沿用历史命名，未随发布状态改名），
由 `deploy/build_release.sh` 一并构建并单独保存为首次安装依赖 tar；Dashboard
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
tools/mapping_validation/run_validation.sh
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
- 稳定多视角运动后 `bundle_adjustment_runs` 增加，
  `bundle_adjustment_final_rmse_px` 小于 `bundle_adjustment_initial_rmse_px`；无重复
  地标、求解过期或超过修正门限时只增加 rejection，不覆盖当前 Pose 或地图。
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
  30 Hz Pose。2026-08-17 起全部勾选 topic 由同一个原生 MCAP recorder 写成标准 rosbag，
  不再按相机分 part。
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

## 2026-10-28 整链离线对照

使用现有含运动包和静止包，从原始双目和完整频率 VIO 重建地图，分别运行建图、
后台 BA、定位、共识及 EKF；两版本共用生产 TensorRT 特征、相同输入和服务延迟，
建图与定位独立进程运行。输出 30 Hz，输入图像缓存 2 Hz；此验证不代表 DDS/GPU
吞吐验收。独立真实黑边角点测板，边长 55 mm、间距 16.5 mm；不拟合坐标、不按
残差裁切，板不参与算法约束。比较完全相同的有效样本。

| 相机对 | 静止位置 RMSE 旧→新 mm | 含运动位置 RMSE 旧→新 mm |
|---|---:|---:|
| 头部—腕 A | 2.407 → 0.994 | 4.350 → 2.426 |
| 头部—腕 B | 2.359 → 0.860 | 118.658 → 20.035 |
| 腕 A—腕 B | 1.401 → 1.402 | 113.216 → 19.542 |

位置 P95 和时间波动在六组比较中均改善；静止腕间 RMSE 基本持平。静态旋转 RMSE
尚未改善（头—A 0.693→0.769°、头—B 0.313→0.318°、A—B 0.736→0.783°），
含运动腕 B 仍约 2 cm。本轮按用户确认的位置口径部署，保留角度问题，不作完整
六自由度通过结论。指标表征公共板心的一致性，不是外部世界或手部末端绝对精度。
