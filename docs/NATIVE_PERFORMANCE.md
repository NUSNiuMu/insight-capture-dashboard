# 本机 Native Qt 性能记录（2026-10-10）

优化前基线：`native-qt-stable-2026-10-10`，提交 `723ed28`，标注“稳定版本地 Qt”。本机另存二进制及现场配置于 `~/workspaces/insight_capture_tests/native_optimization_20261010/stable/`。源码标签不保存现场配置或编译产物。

## 链路与选择

| 使用场景 | 图像路径 |
| --- | --- |
| 本机 Insight3 | 现有 ROS reader → 单帧最新队列 → loopback WebSocket → appsrc → VIC → NVMM 原生显示 |
| 本机 EgoHalo | 现有 ROS reader → 有界连续 GOP 队列 → loopback WebSocket → appsrc → NVDEC → VIC → 原生显示 |
| 远程网页、远程 Qt、旧后端、旧 Insight9 JPEG | 原有 WebRTC 编码/转发路径 |
| MP4 回放 | 原有文件解码与录制时间轴，保留正常 DPB |

默认 `./native_dashboard/run.sh`。本机通道只对 loopback 请求开放，不增加 DDS 图像订阅，也不改相机格式、USB 传输、录制数据或建图输入。Qt 对 `127.0.0.1`、`localhost`、`::1` 自动选择本机通道；对照旧链路：

```bash
INSIGHT_NATIVE_TRANSPORT=webrtc ./native_dashboard/run.sh
```

原始帧只保留最新一帧；H.264 不能任意抽 P 帧，积压、超龄或参考帧丢失后等待完整 IDR 恢复。主机预览队列时限和 Qt 入队前帧龄检查为 150 ms，阻塞 WebSocket 写超时也是 150 ms；这些是丢弃/恢复策略，不是整条链路的端到端延迟保证。断流还受设备 IDR 周期和重连影响。离线卡片释放媒体连接，恢复后按 API 在线状态重连。

本机协议 `/ws/native?camera=<name>&fps=<5..30>`：32 字节网络字节序头 `!4sBBHIIQQ`，依次为 `INPV`、版本 1、格式（1 GRAY8、2 NV12、3 JPEG、4 H264）、保留位、宽、高、源时间戳 ns、主机队列收到时间 ns；后接完整帧。H.264 保留依赖帧，不按请求 FPS 丢压缩帧。

## 其他优化

- 任务列表与录制状态 GET 的磁盘读取移到线程，避免阻塞同一事件循环里的姿态 WebSocket。任务统计缓存由本进程元数据写入立即失效；目录签名辅助识别外部修改，30 秒刷新兜底覆盖原地编辑或合并的文件系统时间戳。分配录制序号仍实时读取。
- 轨迹只在变化且可见时重建，频率上限仍为 10 Hz；姿态、模型配置、夹爪和轨迹分别通知。夹爪节点查找缓存，模型重新加载时清除。
- 保留轨迹显示最多 4096 点，超限均匀抽稀并保留起止点；隐藏期间保留逻辑轨迹，重新显示时更新。原始录制轨迹不抽稀。
- 录制先进入原有 writer，再投递本机预览；本机预览不经过旧 WebRTC 的录制期间 10 FPS 预览节流。HTTP JPEG 备用图像低频刷新，网页 WebRTC 仍可独立请求。

## 现场结果与口径

Ego 已按用户安排拔下；只有 Insight3 A/B 在线。唯一正式后端、建图定位服务和一个 Qt 客户端，逐帧诊断开启，预热后采样 90.10 秒。

| 指标 | Insight3 A | Insight3 B |
| --- | --- | --- |
| 显示 sink 输出率 | 29.99 FPS | 30.00 FPS |
| 主机预览队列收到 → sink 入口中位 | 5.99 ms | 5.67 ms |
| 同一阶段 P95 / 最大 | 8.92 / 16.92 ms | 8.33 / 13.36 ms |
| sink 帧间隔 P95 / 最大 | 41.02 / 51.18 ms | 38.34 / 43.61 ms |
| 大于 100 ms 帧间隔 / sink 丢弃 | 0 / 0 | 0 / 0 |

姿态推送约 30 Hz，最大间隔 53.95 ms。任务列表抽样 9 次，中位 5.37 ms、最大 10.37 ms；录制状态抽样 9 次，中位 7.49 ms、最大 12.40 ms。此前同机只读抽样中，任务扫描约 52–69 ms，姿态推送出现每 5 秒一次、最大约 182 ms 的停顿。

WebRTC worker CPU 从此前约 25% 单核降至约 0.03%，因为本机客户端不再请求编码。Qt 约 43% 单核；与此前约 45% 的诊断条件不同，不据此声称 Qt CPU 大幅下降。GPU 动态频率下忙碌比例中位约 64%，本次未证明 GPU 总负载下降。

上述图像耗时按同主机单调时钟配对，起点是本机预览队列收到帧。**不包括曝光、设备处理、USB/DDS 到 callback 前的耗时和屏幕扫描输出**。sink 输出计数不等于显示屏物理呈现次数，也不能与此前 Ego 源域接收到 sink 的数字直接比较。

## 诊断与验证

```bash
INSIGHT_LATENCY_TRACE=1 ./native_dashboard/run.sh --fps 30 --fullscreen \
  --diagnostics /tmp/native-latency.json
```

诊断 `transport` 为 `local`、`webrtc` 或 `replay`；`native_received` 是 Qt 入队次数，`rendered` 是 sink 输出次数。`frame_trace` 的 `received_ns`、`native_input_ns`、`sink_input_ns` 可配对分析本机路径；只有开启逐帧诊断才记录这些点。默认关闭，避免持续开销。

`/api/cameras` 的 `native_preview.sessions` 表示本机订阅数；`received`、`selected_packets`、`replaced`、`expired` 分别表示入队、交给发送协程、最新帧覆盖和过期丢弃。`selected_packets` 不能当作已发送或已呈现 FPS。H.264 另有 IDR 等待、溢出和参考关系计数。

验证包括实际两台 Insight3、116 项 Python 回归通过，另有 Qt C++ 几何/模型检查及实窗操作。覆盖真实 loopback 二进制 WebSocket、录制优先 callback、元数据缓存失效、慢扫描期间事件循环持续响应、H.264 超龄 IDR 恢复、隐藏/静止轨迹及 12000 点保留轨迹回归。隔离无 ROS 后端使用两路动态灰度帧和四份同一 Ego 原始 H.264 样本，验证六路同时硬件显示、三模型、旧式轨迹快照与 MP4 暂停/跳转/继续/返回实时；模拟中也验证 Ego 离线时四路连接释放、恢复后自动订阅；该模拟不能替代四个真实设备视角的六路验收。

完整诊断、测试脚本与截图归档于 `~/workspaces/insight_capture_tests/native_optimization_20261010/`。尚需 Ego 实物重连后的六路性能、现场运动统一定位、长时间录制完整性，以及光学源到屏幕延迟验收。

旧链路对照：实际两台 Insight3 的 Qt WebRTC 解码显示通过。网页无头检查覆盖页面、API、姿态 WebSocket 与两个 544×640 JPEG 备用图像和本机 Qt 共存，无页面异常；开发镜像 Chromium 的接收 codec 列表不含 H.264，因此显式禁用 RTC 验证备用路径，未把无头浏览器作为网页 H.264 呈现验收。
