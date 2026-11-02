# 项目结构

- `native_dashboard/`：Qt 八个入口，原生图像、空间轨迹、任务、录制库、评分、WiLoR、COLMAP、录制与设置。
- `web_dashboard/`：轻量手机采集页，保留任务、校准、按需预览与手动合包。
- `insight_capture/api/`：两端共用的 HTTP/WebSocket。
- `insight_capture/runtime/`：ROS 输入、LooperVIO 接入、时钟、任务、录制、双端合包与中断恢复。
- `insight_capture/media/`：图像解码、Jetson 编解码、Qt 直通及手机 WebRTC。
- `insight_capture/postprocess/`：录制库、手动同步回放、完整性/轨迹评分、离线 WiLoR 与 COLMAP。
- `insight_capture/core/bag.py`：每条录制一个标准 MCAP，内部多 Chunk；不读取旧组合包、SQLite 或多文件分包。
- `insight_capture/voice/`：本地唤醒、固定指令、识别与播报，不接入外部助手。
- `native_mapping/`：唯一在线 C++ 建图/定位核心；其测试和 SuperGlue 推理构建工具保留。
- `config/devices/`：仅 Insight9 与 EgoHalo 两套配置。
- `tools/device_cli/`、`tools/diagnostics/`：设备与系统运维。
- `tools/mapping_validation/`：在线推理构建及标定板外参采集工具，不含稠密地图或 RViz 服务。
- `deploy/`：镜像、Qt 产物交付、配置联动和升级。

UMI Zarr/LeRobot 导出、夹爪开合追踪、设备手部骨架叠加、固定检测位、Lite 配置、OpenClaw 和假位姿模式已删除。定位图像夹爪遮罩、离线 WiLoR 手姿、COLMAP、原始录制保留。回放无后台预生成或排队；用户手动选择录制并准备。
