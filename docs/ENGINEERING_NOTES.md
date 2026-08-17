# Engineering Notes

本文记录影响 Insight Capture Dashboard 实现方式的实机结论。代码旁只保留当前必须遵守的约束；测量背景和历史原因集中在这里，避免关键路径被长篇排查记录淹没。

## 采集优先级

- ROS 图像 callback 只负责轻量状态更新、录制 writer 投递和 latest-frame 交接，不执行编码或磁盘 I/O。
- 图像由主进程现有 DDS reader 直接交给 `InProcessBagWriter`，不能恢复为额外的 `ros2 bag record` 图像订阅，否则会与预览、WebRTC 竞争。
- 宸境自然语言语音助手运行在宿主机独立进程。SenseVoice INT8 同时负责中文唤醒词与命令转写，
  Silero VAD 只负责本地语音分段，
  Piper 离线播报；原始音频不能进入 ROS
  callback、Dashboard 或 OpenClaw。常驻监听中精确匹配的录制与在线校准短指令由语音桥
  直接访问 loopback Dashboard API，并播放服务启动时生成的缓存回复，不需要唤醒；
  “宸境”只打开 OpenClaw 模式，随后一句固定发送给 OpenClaw。自动停止接口只允许结束
  `looper_record_*`，避免误停网页、
  手势或其他控制器创建的录制。
- 语音“开始校准”成功 reset 后，后台监控必须先观察到本轮未完成状态，再以 Insight3 A、B
  首次同时 `localized=true` 作为完成条件并只播报一次。所有语音播放共用进程锁，避免
  “我在”、命令回复和异步“校准完成”互相争用；输出必须通过 PulseAudio 共享 E3，不能
  直接用 `aplay` 与桌面 speech-dispatcher 抢占 ALSA device。“开始录制”只有在确认语音
  完整播放后才算成功；OpenClaw 自然语言工具路径必须比较调用前后的录制状态。播放失败时
  仅对本轮新出现的 `looper_record_*` 通过受限 automation stop 持续回滚，不能停止网页、
  手势或其他控制器的录制，且播放异常不能使常驻语音进程退出。
- 固定检测位质量门逐台比较全局 Pose 与 version 2 检测位基准，不再用两两相对位姿抵消
  公共坐标变化。Insight3 A/B 使用严格阈值，Insight9 稀疏地图 Pose 使用宽阈值；两路
  Insight3 必须在本轮地图中完成过全局定位，但允许检测时处于 `vio_only` 连续跟踪，不要求
  瞬间 `map_matched`。检测还必须满足 Pose 新鲜和各自稳定窗口；结果写到
  `outputs/results/capture_checks/` 并关联最近一条 bag。version 1 相对基准不能静默迁移，升级
  后必须在确认校准正确时重新保存一次检测位。
- 唤醒和命令录音是两个独立 capture 周期：Silero VAD 等待 0.5 秒静音后，
  常驻 SenseVoice 只接受整句“宸境”或它的配置同音转写，然后播放启动时缓存的“我在”，播放结束后才重新
  打开命令录音。禁止把唤醒词
  尾音继续交给命令识别，也禁止回答后自动打开 follow-up 窗口；每次唤醒只处理一句话，
  避免周围谈话被误当成第二条命令。OpenClaw 语音请求固定使用 Luna、thinking off 和
  fast mode；默认 agent 不加载无关技能，避免为短问答注入额外提示。
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

- 新录制使用 MCAP：三路 dashboard 主图各有一个进程外 writer，其余 topic 共用一个
  `ros2 bag record`。停止时只排空并原子发布四个 part，不复制 payload。SQLite 的 WAL +
  `synchronous=OFF` 仅保留给旧 bag 与恢复兼容，不能改回 `NORMAL` 或 `FULL`。
- 图像 header timestamp 通过录制开始时的固定偏移映射到 recorder timeline，并在录制期间持续审计帧间隔。
- Prepared playback 必须使用 rosbag record timestamp 对齐图像与 Pose。不同相机的
  `header.stamp` 可能分别来自 Unix/NTP 和设备启动时钟，只能用于各 topic 内的节拍审计，
  不能直接求跨相机共同时间段；修改该规则时必须保留跨 header 时钟域回归测试。
- 人工质检使用每个 rosbag 自带的 `review/` 派生包：三路图像只扫描一次并拼成一个
  1280×720、30 Hz H.264 视频，Pose 保持固定 30 Hz 清单。浏览器只解码一个视频并以
  `video.currentTime` 驱动 3D，禁止重新引入三路 `<video>` 的独立时钟和变速追赶。
- 回放准备通过统一的 composite reader 从 SQLite/MCAP 读取时间戳和所需 topic，时间轴
  扫描阶段不能反序列化图像 payload；
  大于输出需求的 JPEG 使用解码器原生半尺寸解码。Jetson 编码路径向 `nvvidconv` 提交
  BGRx，避免整帧 CPU 色彩转换。`manifest.json` 保留各阶段耗时，便于发现性能回退。
- 审阅包在复合会话发布后排队预生成，录制开始时必须暂停；不能从图像 callback
  生成，也不能让质检缓存继续占用 `outputs/` 所在的系统盘。历史包通过 3D 页的
  `Prepare all` 或 `scripts/build_review_bundles.py --all` 补建。
- 2026-08-12 实机验收使用 15.03 秒三相机 bag：NVENC 生成 451 帧 30/1 fps 的
  1280×720 单视频，三路重复帧均为零，成品 11.3 MB；旧三视频缓存合计约 25.3 MB。
  Firefox 原生媒体计数为 451 总帧、1 丢帧（约 29.93 fps）。Firefox 会节流
  `requestVideoFrameCallback`，因此审阅 FPS 必须按 `totalVideoFrames/droppedVideoFrames`
  的媒体时间增量计算，不能把回调频率约 24 fps 误判为视频掉帧。
- `/tf_static` 是 latched 单次流，只要求至少存在一条消息，不按 FPS 判断完整性。
- 400 Hz IMU 使用 `config/rosbag_qos_overrides.yaml` 的 best-effort、
  keep-last 1000 深度 reader；默认深度不足时，短时 CPU 调度停顿会先表现为
  单路 IMU 分散丢帧，而图像 writer 仍显示 0 drop。
- 全局 Pose 与各自相机小消息共用 recorder；完整 Path 是 Pose 可重建的冗余
  调试数据，不默认录制。不要为全局 namespace 再增加两个 recorder 进程。
- staging 恢复中的 reindex、salvage、convert 和输出验证是一个完整流程；`ros2 bag convert` 成功返回不代表输出一定可信。
- 正常录制禁止合包重写：各 MCAP part 完成 metadata 后写入 `recording_manifest.json`，再将
  staging 目录原子改名为最终会话。旧 SQLite staging 的故障恢复仍可使用 reindex、salvage
  和官方 convert，但输出必须单独验证，不能把 convert 成功返回视作可信。
- 旧的固定命令 Vosk worker 默认关闭，避免它与宸境同时独占 USB capture device。
  宸境由 systemd user service 监管，缺少模型或声卡时按服务重启间隔恢复；不得把
  SenseVoice 模型反复加载到每个命令周期，常驻进程只在启动时加载一次。服务启动应把
  USB PCM 音量恢复到 40%，避免声卡重连后的硬件默认值覆盖用户体验。Piper ONNX 会话
  也必须常驻复用；启动时生成“我在”可同时完成预热，回答阶段不得重新启动 Piper CLI。
- 数据集连续性门发现坐标跳变时不得直接低通平滑。孤立、持久的刚体坐标重置只有在
  跳变前后各自稳定、切段后满足最短 episode 且双臂公共坐标关系可重新确认时，才能丢弃
  边界保护帧并按段重锚；短时振荡、多次米级跳变或仅单臂全局 Pose 失配应保留原 bag，
  改用本地 VIO 加稳健的 map-to-VIO 对齐离线重建，无法验证时必须重录。

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

- 新 WebSocket 客户端首包收到完整轨迹快照，正常广播只发送新增轨迹点。前端按
  sequence 累积，发现 generation 或序列缺口会主动重连重新取快照。Dashboard 当前
  以 30 Hz 广播，不得恢复为每次广播重发三路
  完整轨迹。
- 轨迹渲染必须在建立 WebSocket 前启用；连接首包是完整快照，如果为了首屏分阶段
  启动而丢弃首包，后续增量包没有本地基线，页面将一直无轨迹。
- Jetson kiosk 的 3D 场景以独立的 wall-clock timer 固定在 30 Hz。实测中
  `requestAnimationFrame` 即使单帧场景工作不足 10 ms，仍会在 Firefox 合成繁忙时
  出现 67–119 ms 的 callback 间隔；固定 timer 在相同负载下维持约 30 Hz。
- 3D 每帧只插值最新 pose；轨迹 mesh 最多 10 Hz 更新，避免轨迹几何重建与模型移动
  抢占同一帧。正常与回放模式的 Babylon hardware scaling 保持 1.0，按 canvas
  CSS 尺寸原生渲染；只有显式开启 OBS mode 时才以 2.0 降载。
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

同日的第四轮后台 CPU 优化不改变 5 Hz 建图上限、30 Hz Pose 或 5 Hz TF：

- mapper 在一次输入尝试开始时推进 5 Hz deadline；缺 Pose 或静止未形成关键帧时，
  不再对每个双目帧重复等待和变换。
- status 继续以 2 Hz 发布；完整稀疏点云只在地图变化时以最高 1 Hz 发布，稳定地图
  每 10 秒刷新一次供晚加入的 volatile RViz subscriber 使用。
- mapper 和两路 localizer 的 200 点调试 Path 降至 2 Hz，TF 使用独立的 5 Hz timer，
  避免为保持 TF 新鲜度而重复重建和序列化整段轨迹。
- jetson-nx 实机中，修改前 mapper 在 `pose_ready=false` 时单次采样为 60.83%；修改后
  四次采样均值为 46.99%，期间实际完成了一个 228.1 ms 关键帧。ROS 实测稳定点云
  0.095 Hz（约 10.5 秒间隔）、Path 2.00 Hz，独立 mapper TF 六秒窗口为 4.31 Hz。
- RViz 使用的确认点云和三条完整 Path 后续统一由 `--publish-debug-topics` 控制，默认
  不创建 publisher 或 Path timer；内部重定位所需 feature map、30 Hz Pose、5 Hz TF、
  status 和网页基于 Pose 累积的轨迹不受影响。

## 训练数据导出

- `/umi-dataset` 的标准归档格式是 HiFi-UMI 风格 LeRobot v3；Legacy UMI Zarr
  只为旧 Diffusion Policy 训练栈保留。两种导出都在 recorder timeline 上按
  20 Hz 对齐图像、TCP pose 和米制夹爪宽度。
- LeRobot 双臂固定使用 `[left_10d, right_10d]` 20 维 state/action。`action` 是官方
  LeRobot 键，`actions` 是数值相同的 OpenPI 默认键；二者都是下一帧绝对 state，
  模型特定相对动作只能在 training adapter 中转换。
- 夹爪宽度相邻帧跳变超过 30 mm 时不修改有限原始值，只把对应 state/action 维度的
  validity 标为 false。
- 只有明确的数据质量拒绝（连续性、有效帧、解码或夹爪检测导致零有效 episode）才把
  源 rosbag 重命名为 `fail_<原名>` 并从 Dataset 页面隐藏；禁止自动删除。配置、标定、
  权限、磁盘或程序错误必须保留原名，避免误隔离可恢复数据。
- 双臂导出要求两侧 `width_calibration` 和两路全局 pose；单臂使用本机原始
  `vio_100hz`。每个 profile 的默认录制选择必须同时保留原始 VIO 与 dashboard pose。
- TCP 平移连续性门以 5 cm 同时作为候选步长和局部速度创新阈值；只有大步长相对前后
  速度也不连续时才判为坐标跳变，不能把持续高速运动仅按 20 Hz 单帧位移误删。

## 验证基线

2026-07-23 重构前的只读基线：

- `insight3_a`：约 20 FPS，544×640。
- `insight3_b`：约 20 FPS，544×640。
- `insight9_a`：约 30 FPS，1088×1920。
- 三路相机均报告 WebRTC 可用。
- `/3d`、`/recording`、`/bags`、`/umi-dataset`、`/scoring`、`/handpose`、
  `/optimization`、`/settings` 均能加载，WebSocket 首帧包含三路 pose 和轨迹快照。
- 可重复运行的基线工具位于本机 `~/workspaces/insight_capture_tests/run_refactor_checks_20260723.sh`，不属于主仓库。
