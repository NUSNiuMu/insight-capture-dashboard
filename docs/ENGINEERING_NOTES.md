# Engineering Notes

本文记录影响 Insight Capture Dashboard 实现方式的实机结论。代码旁只保留当前必须遵守的约束；测量背景和历史原因集中在这里，避免关键路径被长篇排查记录淹没。

## 采集优先级

- ROS 图像 callback 只负责轻量状态更新、录制 writer 投递和 latest-frame 交接，不执行编码或磁盘 I/O。
- 图像由主进程现有 DDS reader 直接交给 `InProcessBagWriter`，不能恢复为额外的 `ros2 bag record` 图像订阅，否则会与预览、WebRTC 竞争。
- Insight3 在线重定位同样不能再直接增加两个全速原图 DDS reader。dashboard
  复用既有 reader，以 2 Hz 发布 `/insight_mapping/...` 定位图；localizer
  只订阅该中继。实机 A/B 测试中，这使 Insight3 B 从 13–14 FPS 恢复到 20 FPS。
- 当前机队在 2026-07-13 的实测中，图像 QoS 使用 `best_effort` 会因单个 UDP 分片丢失而丢掉整帧；设备配置选择 `reliable` 后，多轮录制没有再观察到对应散落丢帧。
- 2026-08-04 的 51-topic 实测确认 jetson-nx 上 FastDDS 相机发送端会产生 IP
  分片/可靠重传风暴：SSD iowait 与 writer queue 均为零，但 Insight3 图像严重断流。
  三台相机切到 CycloneDDS 后，44 个实际发布 topic 的 300 秒阶梯测试中，
  `ipfrag_max_dist=1024` 仍累计 342 次重组失败；4096 下 softnet、NIC、IP
  reassembly 与 UDP 丢失计数全部为零，writer queue 也为零。此组合由
  `camera_dds_type`、`host_setup.sh` 和开机相机恢复 unit 共同保持。
- 同一轮 4096 长测仍观察到 Insight3 B 的一次同步源节拍缺口：infra1/infra2、
  camera_info 与 VIO image 在同一 header 时间点共同缺样；depth 也有固有的
  120 ms 间隔。它们发生时主机网络与 writer 计数均为零，不能归因于 SSD 或
  recorder。完整性报告必须区分“已发布消息未被写入”和“相机未发布样本”。
- jetson-nx 固件广告的七个未校正 `image_raw` publisher 实测持续不发 payload；
  profile 用 `recording_excluded_topics` 将它们从“可录制”目录排除。对应 rectified
  图像是实际数据流，不得据此忽略任何有 payload 的 topic。
- 录制期间预览可以降频，但原始录制帧不能降频。WebRTC 预览当前限制为 10 FPS，停止录制后立即恢复。

## rosbag 可靠性

- SQLite 存储配置使用 WAL 和 `synchronous=OFF`。这是吞吐、断电恢复和数据完整性之间经过实机验证的折中，不能改回 `NORMAL` 或 `FULL`。
- 图像 header timestamp 通过录制开始时的固定偏移映射到 recorder timeline，并在录制期间持续审计帧间隔。
- `/tf_static` 是 latched 单次流，只要求至少存在一条消息，不按 FPS 判断完整性。
- 400 Hz IMU 使用 `config/rosbag_qos_overrides.yaml` 的 best-effort、
  keep-last 1000 深度 reader；默认深度不足时，短时 CPU 调度停顿会先表现为
  单路 IMU 分散丢帧，而图像 writer 仍显示 0 drop。
- 全局 Pose 与各自相机小消息共用 recorder；完整 Path 是 Pose 可重建的冗余
  调试数据，不默认录制。不要为全局 namespace 再增加两个 recorder 进程。
- staging 恢复中的 reindex、salvage、convert 和输出验证是一个完整流程；`ros2 bag convert` 成功返回不代表输出一定可信。
- 自动 host sync 与合包同属录制收尾；同步完成前禁止开始下一段，避免大 bag 的
  rsync 读盘/CPU 负载与新一段采集重叠。

## WebRTC 与预览

- WebRTC 信令和 H.264 编码运行在独立的 `webrtc_worker.py` 进程，主进程只负责投递经过选择的帧和轮询 health。
- WebRTC 保留相机发布分辨率（只为 NV12 偶数尺寸向下取整），不再根据面板尺寸
  动态降采样。浏览器正常预览请求 25 FPS；主进程在图像转换和 IPC copy 前按
  会话 deadline 选择 latest frame。最高帧率会话直接接受这批已限频帧，只有较低
  帧率会话在 worker 再做一次选择，避免相同 deadline 重复限流。录制期间的
  10 FPS 预览限频仍保留。
- 手部 JPEG 解码、关键点绘制和重编码运行在独立的 `hand_overlay_worker.py`
  进程；首次启用 JPEG 叠加时按需启动，最后一路关闭后退出。
- Dashboard 只为 `dashboard_hand_tracking: true` 的相机订阅手部数据。rosbag
  回放保留 keypoint 以支持手势和 3D 骨架，不回放 hand box。
- 建图点数随 mapper status JSON 发布；Dashboard 不订阅完整稀疏 PointCloud2。
- JPEG HTTP 轮询仍是 WebRTC 失败或浏览器不支持 H.264 时的 fallback；`/api/images/capabilities` 中的 `active_path` 描述的是该 JPEG fallback 能力。
- worker 异常应先检查 `outputs/webrtc_worker.log`、`outputs/hand_overlay_worker.log` 和 health，不应仅凭页面卡顿判断主进程故障。

## 建图一致性

- Insight9 回环接受后必须同时更新关键帧位姿和历史地标，不能退回成只修正统一
  `T_map_odom` 的后续输出；否则旧地图形变会继续传给 Insight3 重定位。
- 位姿图运行在 mapper 的单独推理 worker 内。图优化和地图重融合期间允许丢弃
  过期的 latest-frame 输入，但不得把优化搬进图像 ROS callback 或录制路径。
- 最新 Pose/TF 使用 EKF 平滑后的校正，历史 Path 使用按时间插值的图校正；两者
  的用途不同。回环重建完成后，特征地图必须整体替换并重新发布。
- 当前 600 关键帧是显式资源边界；描述子以 FP16 保留，重建时恢复 FP32。完整
  BA 尚未实现，不应把位姿图合成残差当作真实空间厘米级验收结论。

## 浏览器渲染

- 新 WebSocket 客户端先收到完整轨迹快照，正常广播只发送新增轨迹点；服务端每
  2 秒补发快照用于自愈。前端按 sequence 累积，发现 generation 或序列缺口会主动
  重连重新取快照。Dashboard 当前以 30 Hz 广播，不得恢复为每次广播重发三路
  完整轨迹。
- Jetson kiosk 的 3D 场景以独立的 wall-clock timer 固定在 30 Hz。实测中
  `requestAnimationFrame` 即使单帧场景工作不足 10 ms，仍会在 Firefox 合成繁忙时
  出现 67–119 ms 的 callback 间隔；固定 timer 在相同负载下维持约 30 Hz。
- 3D 每帧只插值最新 pose；轨迹 mesh 最多 10 Hz 更新，避免轨迹几何重建与模型移动
  抢占同一帧。Babylon hardware scaling 和模型分辨率保持原值，不用降画质换帧率。
- mapper 的大 point/descriptor cloud 发布曾在默认 ROS callback group 中阻塞 VIO
  relay，造成最长约 355 ms 的 pose 停顿。VIO 使用独立 callback group 和双线程
  executor 后，实测最长间隔降至约 69 ms；不得重新合并为单线程 executor。
- 三路 WebRTC 会话请求 25 FPS，为 Firefox 全分辨率合成留出传输余量，使可见帧率
  中位数不低于 20 Hz。主进程在图像转换和 IPC copy 之前按会话 deadline 丢弃无用
  预览帧；录制 writer 与原始图像路径不受影响。
- mapper、localizer 与 dashboard 的 pose 频率统一为 30 Hz。向 30 Hz 页面发送 50 Hz
  pose 只增加 DDS/序列化和 callback 工作，不改善可见运动。
- mapper 与 localizer 仍订阅原始高频 VIO，但只以 60 Hz 构造并保存插值样本、以
  30 Hz 推进滤波和发布 pose；稳定 deadline 避免 100 Hz 输入按简单间隔判断时混叠
  成 25 Hz。60 Hz buffer 的相邻样本最大间隔保持在 20 ms，满足图像插值等待窗口。
- 页面先接通 pose、相机和轨迹，再分阶段加载 Avatar；同阶段模型允许并发，较大的
  GLB 不得阻塞 pose/轨迹处理。
- `app.js` 曾被所有页面共同加载，任何顶层异常都会影响全站。重构后每个页面使用独立入口，共享能力通过显式模块导入。

2026-08-05 的 jetson-nx 实机验收（未改变模型/hardware scaling）：

- 30 秒三路视频累计解码 24.0–25.1 FPS，新增 WebRTC 丢包和解码丢帧均为零；
  `requestVideoFrameCallback` 可见帧率中位数为 21.38、21.49、20.00 FPS。
- 3D 帧率中位数 30.02–30.04 FPS，区间 29.75–30.38 FPS，最大帧间隔 53 ms。
- 整机 CPU busy 从约 85–88% 降至 73.3%；OpenBLAS 单线程使 localizer 从
  83–102% 降至约 46%，提前限流使 WebRTC worker 从 43–50% 降至约 32%。
- GPU 仍因 Babylon/WebRender 合成与 SuperGlue TensorRT 推理在 12–89% 间波动；
  暂停 SuperGlue 的 A/B 测试没有改善 3D 卡顿，因此不能用停定位换显示流畅度。

同日的第二轮进程级优化保持三路 25 FPS、原分辨率、30 Hz 3D 和全部建图服务：

- mapper 进程的 15 秒 CPU 均值约从 64.0% 降至 55.5%，localizer 从 44.4% 降至
  40.3%；全部相关进程合计约从 332.0% 降至 323.4%。Firefox/WebRTC 的短窗口结果
  会受合成和编码相位影响，没有以降低画质或帧率换取数字。
- 稳态三路编码 24.7–24.9 FPS、可见帧率 21.6–23.2 FPS，3D 约 30 FPS；累计新增
  解码丢帧和 WebRTC 丢包均为零，ROS pose 实测 29.96–30.00 Hz。
- GR3D 的长窗口均值约 61%，范围 18–97%，与优化前约 58% 的脉冲负载相比没有
  可证实下降；它仍由 Firefox/Babylon 合成与 SuperGlue 推理共同占用。
- Compose 为 dashboard 启用 init 子进程回收；重启旧 kiosk 后，容器内 zombie
  从 24 个降为 0。前端轮询只在内容变化时写 DOM，避免重复触发布局和样式失效。

同日的第三轮 CPU 优化保持原有 ratio test、mutual-best 和 PnP 接受规则：

- Insight3 收到新特征地图时只归一化一次描述子，定位时直接复用不可变缓存；mapper
  回环使用的 `LandmarkMap` 描述子本身已在插入、融合时归一化，也不再重复计算。
- 描述子相似度按 256 个 query 分块，逐块保留 query top-2 和 map mutual-best；跨块
  并列值仍选择首个 query。随机尺寸、空输入、并列值和完整 PnP 的结果均与原算法一致。
- 两路 Insight3 各自使用独立状态锁，特征地图另用 map lock；TensorRT IPC、描述子匹配
  和 PnP 均在锁外运行，避免一台相机的 VIO/图像 callback 阻塞另一台。
- 1024×20000 合成匹配中，完整矩阵核心耗时 821.88 ms，缓存归一化后的分块实现
  724.11 ms；原实现每次额外归一化地图的中位耗时为 13.74 ms。512×10000 场景的
  结果校验值完全相同，进程峰值 RSS 约从 164 MiB 降至 124 MiB。

## 训练数据导出

- `/umi-dataset` 的标准归档格式是 HiFi-UMI 风格 LeRobot v3；Legacy UMI Zarr
  只为旧 Diffusion Policy 训练栈保留。两种导出都在 recorder timeline 上按
  20 Hz 对齐图像、TCP pose 和米制夹爪宽度。
- LeRobot 固定使用 `[right_10d, left_10d]` 20 维 state/action；单臂缺失侧填零
  并由 validity mask 标记。`action` 是下一帧绝对 state，模型特定相对动作只能在
  training adapter 中转换。
- 只有明确的数据质量拒绝（连续性、有效帧、解码或夹爪检测导致零有效 episode）
  才能自动删除源 rosbag。配置、标定、权限、磁盘或程序错误必须保留源数据。
- 双臂导出要求两侧 `width_calibration` 和两路全局 pose；单臂使用本机原始
  `vio_100hz`。每个 profile 的默认录制选择必须同时保留原始 VIO 与 dashboard pose。

## 验证基线

2026-07-23 重构前的只读基线：

- `insight3_a`：约 20 FPS，544×640。
- `insight3_b`：约 20 FPS，544×640。
- `insight9_a`：约 30 FPS，1088×1920。
- 三路相机均报告 WebRTC 可用。
- `/3d`、`/recording`、`/bags`、`/umi-dataset`、`/scoring`、`/handpose`、
  `/optimization`、`/settings` 均能加载，WebSocket 首帧包含三路 pose 和轨迹快照。
- 可重复运行的基线工具位于本机 `~/workspaces/insight_capture_tests/run_refactor_checks_20260723.sh`，不属于主仓库。
