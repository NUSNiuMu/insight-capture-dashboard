# 项目结构与渐进整理规范

本仓库同时包含 ROS 2 节点、Web 后端、浏览器前端、离线处理工具和 Jetson
部署脚本。目录整理的目标不是追求某个通用模板，而是让每项功能有明确归属、
依赖方向可预测，并且现有设备可以持续运行。

## 采用的原则

1. **按业务能力分包，而不是按文件类型堆放。** 手部感知、录制与回放、
   地图定位、离线处理分别形成自己的包；一个包内可以同时有模型、服务和
   辅助算法。
2. **入口稳定，内部渐进迁移。** Docker、Compose、运维脚本或外部调用仍可
   使用原有顶层入口。旧模块先缩成兼容 facade，确认调用方迁移完后再决定
   是否删除，禁止一次性全仓改路径。
3. **入口只做组合和生命周期管理。** HTTP route、ROS node 入口和 worker
   入口不承载重计算；算法和状态机放到所属领域包。
4. **依赖只向内。** 入口可以依赖领域包，领域包可以依赖小型基础设施；
   领域包不能反向导入 Web route 或具体启动脚本。
5. **源码、运行数据和构建产物分离。** `rosbags/`、`outputs/`、`runs/`、
   `data/` 不进入 Git 或 Docker build context；前端 `dist/` 是本项目明确
   需要提交的部署产物，属于有意保留的例外。
6. **避免万能 `utils.py`。** 代码优先留在拥有它的领域；只有同时被多个
   领域使用、且语义与任何一个领域都无关时，才进入共享基础模块。

这些选择参考了以下官方或行业资料：

- [PyPA：src layout 与 flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
  说明 `src` layout 能隔离可导入代码，但也要求先安装项目。本仓库目前依赖
  直接执行 ROS/Python 入口，因此暂不一次性切换，先在 `scripts/` 下形成
  清晰的 Python 包。
- [ROS 2：开发 ROS 2 package](https://docs.ros.org/en/galactic/How-To-Guides/Developing-a-ROS-2-Package.html)
  建议以 package 表达依赖、入口和可安装单元。等现有直接脚本入口完成兼容
  收敛后，再评估是否转为一个或多个 `ament_python` package。
- [Docker：build context](https://docs.docker.com/build/concepts/context/)
  和 [构建最佳实践](https://docs.docker.com/build/building/best-practices/)
  要求缩小构建上下文、排除运行数据并使用多阶段构建。
- [Strangler Fig 渐进替换](https://martinfowler.com/bliki/StranglerFigApplication.html)
  强调用小步迁移代替高风险的一次性重写。

## 当前目录职责

```text
scripts/
  *_worker.py / *_mapper.py         稳定进程入口；只做参数、I/O 和生命周期
  multi_camera_dashboard_web.py     Dashboard ROS 组合入口兼容面
  post_processing.py                离线处理公共导入兼容面
  lerobot_dataset_export.py         LeRobot v3（HiFi-UMI profile）导出
  umi_dataset_export.py             旧 UMI Zarr 训练栈兼容导出
  webrtc_worker.py                  独立 WebRTC 信令与硬件 H.264 worker
  hand_overlay_worker.py            按需启动的手部叠加 worker
  dashboard_web/                    HTTP app、context、middleware、routes、WebSocket
  dashboard_runtime/                Capture Mode、Preflight、Session/Take、主动 QC、媒体监管
  dashboard_media/                  硬件 JPEG 和 WebRTC 流实现
  hand_tracking/                    viewer 按需手部关键点与夹爪跟踪
  handpose/                         离线 WiLoR Hand pose 提取与任务管理
  post_processing_core/             bag 完整性、评分、录制、恢复、回放、同步、优化与 legacy 兼容
  insight9_mapping_core/            稀疏/稠密建图、位姿图和全局定位算法
web_dashboard/
  src/                              前端源码，按领域和页面分包
  dist/                             后端直接服务的构建产物，必须由 build.js 生成
config/
  devices/                          可提交的设备 profile
deploy/                              客户机部署和升级入口
docs/                                用户、部署和工程决策文档
rosbags/ outputs/ runs/ data/        运行数据；不提交、不烘焙
```

`scripts/` 顶层允许保留两类文件：

- 被 Docker、Compose、运维命令或外部工具直接调用的稳定入口；
- 迁移期间为旧 import 保留的短小 facade。

新的业务实现不得直接增加到 `scripts/` 顶层。

## 新代码放置决策

| 功能 | 放置位置 |
|---|---|
| HTTP endpoint | `dashboard_web/routes/`，跨 route 状态经 `DashboardContext` |
| Dashboard ROS/图像/worker 协调 | `dashboard_runtime/` |
| JPEG 编解码与 WebRTC 流 | `dashboard_media/` |
| 实时手部 landmark、手势、夹爪 | `hand_tracking/` |
| 离线 Hand pose | `handpose/` |
| 录制、bag 完整性、评分、回放、优化 | `post_processing_core/` |
| LeRobot / UMI 数据集导出 | 顶层稳定命令 + `dashboard_web/umi_export.py` 任务协调 |
| Insight9 建图/定位算法 | `insight9_mapping_core/` |
| 浏览器页面级逻辑 | `web_dashboard/src/pages/` |
| 可跨页面复用的前端逻辑 | `web_dashboard/src/shared/` 或明确领域目录 |
| 运维 shell 入口 | 暂留 `scripts/` 顶层；达到一组后迁入 `scripts/ops/` 并保留 wrapper |
| 临时验证数据 | 仓库外测试目录或 `/tmp`，禁止放入源码树 |

如果一个文件同时符合多个位置，优先选择“掌握该业务规则和状态”的领域，而
不是调用它次数最多的入口。

## 依赖方向

```text
process entry / HTTP route
            ↓
       domain package
            ↓
small infrastructure adapter
```

- `dashboard_web/routes` 可以调用 manager，但 manager 不导入 route。
- worker 入口可以调用 `hand_tracking`，`hand_tracking` 不启动 worker。
- facade 只做 re-export，不新增业务逻辑。
- 两个领域需要交互时，通过已有 context、明确的数据对象或回调连接，不互相
  读取私有状态。

## 当前整理基线

- Web 后端已拆为 `dashboard_web/`，运行时协调已拆为 `dashboard_runtime/`，
  JPEG/WebRTC 实现已拆为 `dashboard_media/`。
- 浏览器前端已按 `pages/`、`shared/`、`camera/`、`spatial/` 分包；不存在需要
  继续维护的全站单体 `app.js`。
- 手部实时逻辑位于 `hand_tracking/`，离线 WiLoR 任务位于 `handpose/`；旧
  MediaPipe hand pose 路径已删除。
- `multi_camera_dashboard_web.py` 和 `post_processing.py` 是外部调用仍依赖的
  稳定 facade，不应重新承载业务实现。
- Insight9 稀疏建图、位姿图和 Insight3 全局重定位共享
  `insight9_mapping_core/`；稠密建图仍是内部验证能力。
- 现场默认 Capture Mode；viewer lease 才启用 JPEG/WebRTC/hand display，空闲后回收 worker。
- 语音固定命令、Preflight、Session/Take 和 anomaly timeline 是正式采集控制面；OpenClaw
  只作为可选自然语言增强。

后续整理应按功能小步提交，并验证兼容 import、稳定命令、真实 API/ROS 路径
和相关页面。只有关键路径具备足够自动化覆盖后，才评估引入
`pyproject.toml` 和 `src/insight_capture/`；不能为了目录形式破坏设备上的
直接执行方式。
