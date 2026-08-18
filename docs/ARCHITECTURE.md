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

`multi_camera_dashboard_web.py` 与 `post_processing.py` 继续作为稳定 facade。新运行时状态位于
`dashboard_runtime/{preview_manager,preflight,session_take,active_qc}.py`，HTTP 接口集中在
`dashboard_web/routes/runtime.py`。SQLite/composite 只保留历史数据兼容，新录制不得依赖它们。
