# 项目结构

- `native_dashboard/`：Qt 七个入口，原生图像、空间轨迹、任务、录制库、评分、COLMAP、录制与设置。
- `web_dashboard/`：轻量手机采集页，保留任务、校准、按需预览与手动合包。
- `insight_capture/api/`：两端共用的 HTTP/WebSocket。
- `insight_capture/runtime/`：ROS 输入、LooperVIO 接入、时钟、任务、录制、双端合包与中断恢复。
- `insight_capture/media/`：图像解码、Jetson 编解码、Qt 直通及手机 WebRTC。
- `insight_capture/postprocess/`：录制库、手动同步回放、完整性/轨迹评分与离线 COLMAP。
- `insight_capture/core/bag.py`：每条录制一个标准 MCAP，内部多 Chunk；不读取旧组合包、SQLite 或多文件分包。
- `insight_capture/voice/`：本地唤醒、固定指令、识别与播报，不接入外部助手。
- `native_mapping/`：唯一在线 C++ 建图/定位核心；其测试和 SuperGlue 推理构建工具保留。
- `config/devices/`：仅 Insight9 与 EgoHalo 两套配置。
- `tools/diagnostics/`：启动同步支持与回放诊断工具；通用设备维护 CLI 和系统医生已删除。
- `tools/mapping_validation/`：在线推理构建工具，不含稠密地图或 RViz 服务。
- `deploy/`：镜像、Qt 产物交付、配置联动和升级。

WiLoR、系统医生、设备维护 CLI、旧单相机板评估、稠密双目遗留与 Torch 对照后端，以及 UMI Zarr/LeRobot 导出、夹爪开合追踪、设备手部骨架叠加、固定检测位、Lite 配置、OpenClaw 和假位姿模式已删除。腕部定位遮罩默认关闭；COLMAP 与原始录制保留。回放无后台预生成或排队；用户手动选择录制并准备。
