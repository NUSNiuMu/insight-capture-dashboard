# `scripts/` 目录约定

`scripts/` 同时是当前容器的 Python import root 和稳定进程入口目录。业务实现
应进入领域包；顶层只保留启动入口、管理员命令和迁移期兼容 facade。

新增代码前先查阅 [项目结构规范](../docs/PROJECT_STRUCTURE.md)。

## 领域包

| 目录 | 职责 |
|---|---|
| `dashboard_web/` | aiohttp 应用和 Web API |
| `dashboard_runtime/` | 图像管线、进程内录制桥接、worker 监管、watchdog 和 WS payload |
| `dashboard_media/` | 硬件 JPEG 和 WebRTC 流 |
| `hand_tracking/` | 实时手部感知、手势、夹爪及 rosbag 离线夹爪提取 |
| `handpose/` | 离线 Hand pose |
| `post_processing_core/` | rosbag 完整性、录制恢复、同步、回放、评分与优化 |
| `insight9_mapping_core/` | 稀疏/稠密建图、位姿图和全局定位算法 |

## 关键稳定入口

| 文件 | 职责 |
|---|---|
| `multi_camera_dashboard_web.py` | ROS 生命周期与领域服务组合 facade |
| `inprocess_bag_writer.py` | 复用 Dashboard 图像 reader 写入 SQLite rosbag |
| `webrtc_worker.py` | 独立 WebRTC 信令与硬件 H.264 编码进程 |
| `hand_overlay_worker.py` | 按需启动的手部叠加进程 |
| `insight9_sparse_mapper.py` | 在线稀疏建图入口 |
| `insight3_global_localizer.py` | 双路全局重定位入口 |
| `lerobot_dataset_export.py` | 标准 LeRobot v3 数据集导出 |
| `umi_dataset_export.py` | 旧 UMI Zarr 兼容导出 |
| `post_processing.py` | 离线处理公共导入 facade |

图像录制不能改回额外的 `ros2 bag record` 图像订阅；IMU、VIO 等小消息仍由
录制管理器的子进程负责。新增 Web route 必须在 `dashboard_web/app.py` 注册，
业务处理放入对应 `dashboard_web/routes/`。

## 顶层文件判断

- 外部命令直接执行：可以保留，但应尽量只解析参数并调用领域包。
- 旧代码仍在 import：保留短小 facade，并把新调用改为领域包路径。
- 只被一个功能使用的算法或状态机：移入该功能的领域包。
- 一次性实验或大文件：不要放进仓库。
