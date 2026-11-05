# 架构

Qt 桌面与手机页面共用采集后端；Dashboard 镜像负责 ROS 接入、MCAP 录制、预览、回放、评分与离线 COLMAP。原生 C++ LooperVIO 建图定位和 TensorRT SuperGlue 匹配各使用独立运行镜像。

图像订阅优先保留录制与输入审计，只有观看者存在时才处理 Native/WebRTC 预览。JPEG 编码和派生回放编码仅使用 Jetson 硬件，不维护并行备用 JPEG。

三设备坐标使用统一 30 Hz 时间网格，但各设备按自身有效 VIO 独立发布。纠偏变换按时间网格保存，迟到设备使用同一时刻的历史变换；单路未初始化、缺样本或跟踪异常不再通过发布条件阻塞其他设备。初始世界坐标关系仍需建立；SDK 跟踪异常时的清除及整图重置策略保留，后续单独讨论。

正式入口为 `insight_capture.runtime.app`。服务组装集中在 `insight_capture/composition.py`，ROS 接入位于 `runtime/ros`，定位位于 `runtime/mapping`，媒体位于 `media`，HTTP/WebSocket 位于 `api`，离线处理位于 `postprocess`。

每条录制使用单个标准 MCAP，内部可以有多个 Chunk。EgoHalo 与 Jetson 先分别保存，再手动回传合包；原始两端数据保留。回放只在用户请求时准备。

WiLoR、系统医生、通用设备维护 CLI、旧 ChArUco 测量和旧数据格式兼容入口已删除。保留启动所需的相机准备/同步脚本、COLMAP 和在线 TensorRT 引擎构建工具。
