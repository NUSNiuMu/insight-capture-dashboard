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
- 手部 JPEG 解码、关键点绘制和重编码运行在独立的 `hand_overlay_worker.py`
  进程；首次启用 JPEG 叠加时按需启动，最后一路关闭后退出。
- Dashboard 只为 `dashboard_hand_tracking: true` 的相机订阅手部数据。rosbag
  回放保留 keypoint 以支持手势和 3D 骨架，不回放 hand box。
- 建图点数随 mapper status JSON 发布；Dashboard 不订阅完整稀疏 PointCloud2。
- JPEG HTTP 轮询仍是 WebRTC 失败或浏览器不支持 H.264 时的 fallback；`/api/images/capabilities` 中的 `active_path` 描述的是该 JPEG fallback 能力。
- worker 异常应先检查 `outputs/webrtc_worker.log`、`outputs/hand_overlay_worker.log` 和 health，不应仅凭页面卡顿判断主进程故障。

## 浏览器渲染

- pose payload 包含完整轨迹历史，序列化、网络和浏览器更新成本都随轨迹长度增长。
- 3D 场景限制为 20 FPS，并使用 Babylon.js hardware scaling，避免重复渲染相同 pose 时与多路视频合成争用 CPU/GPU。
- 模型首次加载必须并发进行；串行等待一个较大的 GLB 会阻塞其他相机的姿态和轨迹更新。
- `app.js` 曾被所有页面共同加载，任何顶层异常都会影响全站。重构后每个页面使用独立入口，共享能力通过显式模块导入。

## 验证基线

2026-07-23 重构前的只读基线：

- `insight3_a`：约 20 FPS，544×640。
- `insight3_b`：约 20 FPS，544×640。
- `insight9_a`：约 30 FPS，1088×1920。
- 三路相机均报告 WebRTC 可用。
- `/3d`、`/recording`、`/bags`、`/scoring`、`/optimization`、`/settings` 均能加载，WebSocket 首帧包含三路 pose。
- 可重复运行的基线工具位于本机 `~/workspaces/insight_capture_tests/run_refactor_checks_20260723.sh`，不属于主仓库。
