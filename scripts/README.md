# `scripts/` 目录约定

`scripts/` 同时是当前容器的 Python import root 和稳定进程入口目录。业务实现
应进入领域包；顶层只保留启动入口、管理员命令和迁移期兼容 facade。

新增代码前先查阅 [项目结构规范](../docs/PROJECT_STRUCTURE.md)。

## 领域包

| 目录 | 职责 |
|---|---|
| `dashboard_web/` | aiohttp 应用和 Web API |
| `dashboard_runtime/` | Dashboard 运行时协调 |
| `dashboard_media/` | 硬件 JPEG 和 WebRTC 流 |
| `hand_tracking/` | 实时手部感知、手势和夹爪 |
| `handpose/` | 离线 Hand pose |
| `post_processing_core/` | rosbag 完整性、评分与离线处理 |
| `insight9_mapping_core/` | 建图和定位算法 |

## 顶层文件判断

- 外部命令直接执行：可以保留，但应尽量只解析参数并调用领域包。
- 旧代码仍在 import：保留短小 facade，并把新调用改为领域包路径。
- 只被一个功能使用的算法或状态机：移入该功能的领域包。
- 一次性实验或大文件：不要放进仓库。
