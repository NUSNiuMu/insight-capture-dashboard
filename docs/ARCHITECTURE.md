# Capture-first 架构

系统仍使用单一 Dashboard `Dockerfile`，MCAP 录制、回放、WiLoR、LeRobot 与 Web/Kiosk
共用一个镜像；没有拆分 runtime/postprocess 镜像。SuperGlue、Insight9 sparse mapper 和
Insight3 global localizer 保持正式在线服务。

```text
Field capture
  ROS image readers ─┬─ native MCAP recorder subscriptions
                     ├─ header audit / camera health / active QC
                     └─ 2 Hz Insight3 localization relay
  host voice ────────── Preflight / Recording / Session-Take API

Viewer lease exists
  same image readers ── JPEG fallback / lazy WebRTC / optional hand overlay

Post-capture
  playback / integrity / WiLoR / Ego LeRobot / UMI / LeRobot export
```

正式入口直接使用 `insight_capture.runtime.app` 与各领域模块，不再经由 `scripts/`
中的 Python facade。现场状态位于 `insight_capture/runtime/`，显示管线位于
`insight_capture/media/`，HTTP 接口位于 `insight_capture/web/routes/`。
SQLite/composite 只保留历史数据兼容，新录制不得依赖它们。
