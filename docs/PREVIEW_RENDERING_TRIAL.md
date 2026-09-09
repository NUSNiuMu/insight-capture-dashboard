# Jetson Chromium 硬解与 Babylon 粗线试验

2026-09-09；独立分支 `experiment/preview-rendering`，基于 `54ef063`。试验不修改主分支的 25 FPS 目标，不修改网络、录制、建图或定位配置。

## Chromium 手动开启硬解

现场为 Orin NX、Jetson Linux R36.4.3 / 5.15.148-tegra。使用系统 Snap Chromium 151.0.7922.173 的独立 profile，尝试参数：

```text
--use-gl=angle --use-angle=gl
--enable-features=AcceleratedVideoDecodeLinuxGL,VaapiOnNvidiaGPUs
--ignore-gpu-blocklist
```

- `SystemInfo.getInfo` 返回 `video_decode: disabled_software`、`gpu_compositing: disabled_software`，`videoDecoding` 列表为空。
- 日志包含 EGL `Could not create the initialization pbuffer`，随后 VA-API 报无法取得适用 render node。
- 实际三路 WebRTC 均协商为 H.264，并产生 `framesDecoded`，尺寸分别为 544×640、544×640、1088×1920。能播放不能证明硬解；本次 GPU 状态明确未接通硬解。可选的 `decoderImplementation` 未暴露，不以其缺失推断软解。
- 系统有 `/dev/v4l2-nvdec` 及 NVIDIA V4L2 多媒体库；直接调用系统 `vaGetDisplayDRM` / `vaInitialize` 测试两个 DRM render 节点，均因 `driver_name = (null)` 失败。不能把 Jetson V4L2 codec 接口等同于已可用的 VA-API。

本次结论：**当前安装环境不能仅靠上述 Chromium 参数开启硬解**。这不排除适配浏览器/驱动后的可能性，也不表示 Orin NX 硬件不支持解码。后续需要先解决 JetPack 对应的浏览器 codec 桥接和 EGL 环境，再验真实 WebRTC 解码；本次未安装未知驱动或替换系统浏览器。

参考：[Chromium VA-API 调试与启用说明](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/docs/gpu/vaapi.md)。

## Babylon 粗线原型

本分支的 `/3d?trail=greased` 启用 camera-facing GreasedLine；省略该参数保持原有圆管。采用仓库现有 Babylon 9.14.0，不增加依赖。

- 三维轨迹点和深度关系保留，由 shader 随相机方向展开线宽。与固定平面的 ribbon 不同，转到侧面仍有宽度；正对某段线的端点时仍可能因投影重叠变短。
- 300 点单条圆管：1,800 顶点、3,588 三角形；粗线：600 顶点、598 三角形。减少约 67% 顶点、83% 三角形。
- 第一版每次 `setPoints()` 会重建拓扑和多组缓冲区，实测没有端到端收益。最终原型固定拓扑，更新 position、previous/side、next/counter 三组缓冲区；只改 offsets 会导致邻接点/连接处错误，因此未采用该捷径。
- 原型使用固定颜色、完整显示、无纹理/虚线；重采样、轨迹开关、清空、容量切换、trace generation/sequence 逻辑沿用现有实现。
- 更新依赖 Babylon 9.14 的 `grl_previousAndSide`、`grl_nextAndCounters` 属性布局；升级 vendored Babylon 前必须重新验证。新旧位置邻接数据在 2/17/300 点、开放/闭合共 6 组用例中与库原生 `setPoints` 结果一致。

参考：[Babylon GreasedLine 官方说明](https://raw.githubusercontent.com/BabylonJS/Documentation/master/content/features/featuresDeepDive/mesh/creation/param/greased_line.md)。

## 对照方法与边界

原设备静止时没有可见运动轨迹，因此不能用原场景宣称轨迹优化收益。隔离前端代理连接真实三路视频，只在浏览器收到的姿态流中提供三条各 300 点的合成运动轨迹，20 Hz 消息、10 Hz 轨迹更新，3D 目标 30 FPS。页面显式标注合成轨迹。每组约 24 秒，使用同一可见 Firefox、2560×1440 屏幕，视频目标始终 25 FPS。采集实际累计呈现帧数、rAF 间隔、定时器延迟与场景同步 CPU 工作耗时；这些不等同于 GPU 执行时间。

原始结果与验证脚本保存在设备 `~/workspaces/insight_capture_tests/frontend_fps/`。短时合成轨迹对照不替代真实运动、长时保留轨迹、录制满载验收。

## 结果

| 组别 | 三路实际呈现 FPS：A / B / Insight9 | rAF 间隔 P95 |
| --- | --- | --- |
| 圆管第一组 | 22.73 / 23.21 / 23.31 | 83.30 ms |
| 原生 setPoints 粗线第一组 | 22.50 / 22.67 / 22.29 | 82.96 ms |
| 固定缓冲区粗线第一组 | 22.62 / 22.48 / 22.41 | 82.90 ms |
| 圆管复测 | 23.20 / 23.14 / 23.02 | 83.40 ms |
| 固定缓冲区粗线复测 | 23.53 / 23.53 / 23.81 | 83.42 ms |

完整场景同步 CPU 耗时包含姿态/轨迹更新及 render 调用：复测圆管均值 4.83 ms、P95 12 ms；粗线均值 4.84 ms、P95 14 ms。两组复测都出现远高于场景同步工作耗时的调度长间隔（约 0.9 / 1.7 秒）；单次长尾不能单独归因于轨迹实现，需浏览器原生 profiler 继续分解。视频帧率的变化方向在两轮中不一致，**当前负载下未证明稳定的端到端性能收益**，不能据此宣布已达到 30 FPS。

一组中间采样的汇总脚本曾因新会话尚未产生 `encoded_fps` 抛错，原始采样已保存；未把该组作为上表复测结论。修正采样脚本缺值处理、延长预热后完成复测。

另用 `videotestsrc num-buffers=30` 的 320×240 测试图，经过 `nvvidconv ! nvv4l2h264enc ! h264parse ! nvv4l2decoder ! fakesink`，成功打开 NVDEC（BlockType 261）、播放并收到 EOS，退出码 0。该控制验证说明 Jetson 原生硬解通路可用；与 Chromium 是否支持该接口是两回事。

结论：保留 opt-in 粗线原型用于后续试验，不合并默认行为；正式页面恢复原版圆管和 25 FPS 目标。硬解需要适配当前 JetPack 的浏览器后端/驱动接口，粗线替换本身不能消除已观察到的视频与 3D 合成调度压力。
