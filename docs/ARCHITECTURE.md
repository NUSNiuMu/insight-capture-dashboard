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
`insight_capture/media/`，HTTP 接口位于 `insight_capture/api/routes/`，后台任务
编排位于 `insight_capture/services/`。API route 只做协议转换，不持有线程、
subprocess 或数据集路由逻辑。
SQLite/composite 只保留历史数据兼容，新录制不得依赖它们。

`runtime/app.py` 只负责解析启动参数、触发进程级服务组装并关闭生命周期；跨越
runtime、postprocess、services 和 API 的组装细节集中在 `insight_capture/composition.py`。
`runtime/bootstrap.py` 解析现场路径并创建 recorder，ROS node 实现在
`runtime/ros/node.py`。图像、录制、mapping、payload 和 worker 的具体工作继续委托给
各自模块，不能重新堆回入口。
