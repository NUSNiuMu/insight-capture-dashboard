# Insight 三设备采集系统

项目共用 `main` 和一套后端：本地 Qt 提供完整桌面功能，手机网页负责现场采集控制。桌面网页版与浏览器 kiosk 已移除。

## 日常启动

后端通常已由 Docker 保持运行；需要手动启动时，在 Jetson 宿主机执行：

```bash
./scripts/run_dashboard.sh
./native_dashboard/run.sh
```

Qt 默认连接 `http://127.0.0.1:8765`。退出 Qt 不停止后端，也不停止录制。启动脚本不会临时安装或编译依赖；源码开发时先执行 `./native_dashboard/build.sh`，客户机直接使用发布包里的程序和运行库。

手机与 Jetson 连到可互通的网络后访问 `http://<Jetson-IP>:8765/mobile`，根路径 `/` 也打开同一手机页面。没有桌面 `/3d`、`/bags` 等页面。

## 设备配置

| 配置 | 设备组合 |
| --- | --- |
| `jetson-nx` | Insight9 + 双 Insight3 |
| `jetson-nx-egohalo` | EgoHalo + 双 Insight3 |

```bash
./scripts/select_device.sh jetson-nx-egohalo
./scripts/select_device.sh jetson-nx
```

切换会替换 `config/cameras.json` 和 `config/runtime.json`，按配置启停跨域转发、图像校正与编码服务，并重新加载建图定位和后端。录制、保存或手机操作期间拒绝切换。切换后需要重新校准；不要把服务健康检查当作相机有数据的证明。

配置模板位于 `config/devices/<profile>/`；现场生成配置、`.env`、录制和处理结果不提交。自定义相机地址、外参或参数需要维护在对应模板中，避免再次选择配置时被默认值覆盖。离线准备配置可以加 `--no-start`，此选项不操作 Docker。

## Qt 与手机各做什么

Qt 包含任务、空间采集、录制库、评分、优化、逐话题录制和设置七个入口。录制库支持两端原始数据回传合包。Qt 使用 C++/QML、Quick 3D 与 GStreamer，没有内嵌网页。

手机支持任务选择/创建/结束、录制启停、录制期间校准、设备状态、按需单路预览和手动合包。任务管理与合包位于页面底部的折叠区域，直接复用后端 API。回放、评分、优化和完整设置在 Qt 操作。

EgoHalo 可以先开始录制，再开始校准，保存完整过程。停止只保存两端原始数据，保存完成即可开始下一条；结束采集后在 Qt 录制库或手机管理区手动回传合包。手机断线不停止设备录制。校准板只用于离线误差评估，不参与自然场景建图定位。

建图、三设备融合、VIO、录制和后处理均在同一后端实现，前端不维护第二套算法。详见 [建图定位](docs/INSIGHT9_SPARSE_MAPPING.md)、[EgoHalo 双端录制](docs/EGOHALO_DISTRIBUTED_RECORDING.md) 与 [Qt 功能说明](native_dashboard/PARITY.md)。

## 存储与数据

`rosbags/` 保存录制，`outputs/` 和 `runs/` 保存处理产物。正式部署通过 `INSIGHT_ROSBAG_HOST_DIR` 绑定采集盘，`INSIGHT_ROSBAG_REQUIRED_SOURCE` 核对实际挂载；采集盘不可用时仍明确显示备用存储状态。不要清空这个设置来隐藏存储异常。

回放缓存仅在手动点击“准备并回放”后生成，录制或合包完成不会自动转码。

完整性、掉帧、轨迹评分和定位精度是不同维度；不能用图像连续或 VIO 状态正常替代相对定位验证。原始图像、时间戳、设备 VIO 与全局位姿保留各自含义。定位精度需使用固定标定板的独立观测另行评估。

## 开发与发布

```bash
# Qt 构建环境只在开发/打包机器准备，客户运行时不需要。
./native_dashboard/build.sh --prepare-builder
./native_dashboard/build.sh
node web_dashboard/build.js
./deploy/build_release.sh v2.1.0
```

`web_dashboard/` 仅保留手机页面及其 WebRTC 模块，无前端框架或 npm 运行时依赖。更改源码后必须重建并提交 `dist/`。

后端运行镜像不携带 Firefox、Chromium、Playwright 或 Qt 构建工具；COLMAP、CUDA 和媒体库仍供现有功能使用。原生建图镜像单独携带可执行文件、Ceres 和必要 OpenCV 库，不继承 Dashboard 或离线模型。Qt 程序和运行库随部署包交付，镜像升级也会同步宿主机 Qt 产物。

[部署与升级](docs/DEPLOYMENT.md) 说明首次安装、镜像更新与离线使用。[硬件手册](docs/CAPTURE_BACKPACK_HARDWARE_MANUAL.pdf) 提供硬件操作说明。

## 主要代码

| 目录 | 职责 |
| --- | --- |
| `native_dashboard/` | Qt 桌面源码、构建和启动 |
| `web_dashboard/` | 手机页面源码与构建产物 |
| `insight_capture/api/` | 两端共用的 HTTP/WebSocket |
| `insight_capture/runtime/` | 相机、定位、录制与任务 |
| `insight_capture/media/` | 编解码和预览 |
| `insight_capture/postprocess/` | 合包、回放、质量检查、优化 |
| `native_mapping/` | LooperVIO 原生核心与独立镜像 |
| `deploy/` | 服务选择、镜像构建和客户升级 |

录制与交付统一为每条一个标准 MCAP，内部多个 Chunk。保留 ROS 2 的 `metadata.yaml`；旧组合包、SQLite 与多文件分包读取已移除，既有原始文件不会被清理。EgoHalo 两端原始暂存仍在手动合包时写成一个 MCAP。
