# Insight Capture Dashboard

面向机器人学习数据采集的无屏数采背包：现场以离线语音控制、MCAP 录制、定位和主动
QC 为核心；采后才打开本机 Firefox/Kiosk 或 Web Dashboard 查看三路图像、回放、质检、
处理并导出 LeRobot/UMI 训练数据。
在线空间基准由 Insight9 稀疏建图与 Insight3 全局重定位提供；旧 AprilTag 在线对齐
链路已经移除。

> **交付给客户的使用手册在 [docs/USAGE.md](docs/USAGE.md)**（日常操作、采集
> 流程、故障排查诊断树；不含安装——环境由我们出厂配置好）。
> **怎么打包发布镜像 / 给设备升级 / 全新设备怎么部署，见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**
>
> **叠杯批量采集、每单元检测位复核和异常重录规则见 [docs/CUP_STACKING_DATA_COLLECTION_SOP.md](docs/CUP_STACKING_DATA_COLLECTION_SOP.md)**
> （开发者打包手册 + 使用者升级手册 + 全新 Jetson 首次部署，两条路径）。
> **数采员现场语音操作见 [PDF 指导手册](docs/DATA_COLLECTOR_VOICE_MANUAL.pdf)**
> （8 页，可直接打印；对应的可维护印刷源为 [HTML](docs/DATA_COLLECTOR_VOICE_MANUAL.html)）。
> 内部装机用 `./deploy/setup_host.sh`（幂等：runtime 检查 + CycloneDDS/分片/RPS 调优 +
> 构建 + 启动）；录制后数据完整性检查用 `scripts/check_bag.py`。
> 本 README 侧重功能与配置参考。

当前 dashboard 使用 **Web 版**（`python3 -m insight_capture.runtime.app`）：ROS2/VIO
处理在后端，前端是 Babylon.js 浏览器 GPU 渲染，可以本机接显示器看，也可以远程浏览器连。

`ROS_DOMAIN_ID` 从当前未跟踪的 `config/cameras.json` 读取；jetson-nx profile
默认值为 20。

## 快速开始

### 方式一：docker compose（推荐，持久化服务）

```bash
docker compose build   # 首次构建，之后代码不变可跳过
./scripts/run_dashboard.sh            # 只启动后端，打印 SSH 隧道命令，供笔记本电脑远程连
./scripts/run_dashboard.sh --jetson   # 额外拉起本机全屏 3D kiosk，接了显示器的场景用
```

`run_dashboard.sh` 内部就是 `docker compose up -d` + 健康检查，幂等，可以随时重复跑。容器用 `restart: unless-stopped`，SSH 断开、机器重启后 Docker 会自动拉起来。
镜像内置 WiLoR 权重和 GPU 推理运行时，首次或无缓存构建会下载并导出数 GB
固定内容；日常 Python/前端改动由源码 bind mount 生效，通常只需重启
`insight-dashboard`，不要每次都先执行完整 `docker compose build`。

远程访问：在自己电脑上开 SSH 隧道，不需要暴露端口到局域网：

```bash
ssh -L 8765:localhost:8765 <user>@<jetson-ip>
# 然后浏览器打开 http://localhost:8765/
```

### 方式二：直接跑 Python（开发调试用）

```bash
cd web_dashboard && npm run build   # 改过前端代码才需要重新 build
cd ..
python3 -m insight_capture.runtime.app
```

宿主机和容器内命令完全一样，只是工作目录不同（宿主机通常是 `/home/seeed/workspaces/insight_capture`，容器内 `docker exec` 进去是 `/workspaces/insight_capture`）。

没有 ROS2 硬件时可以用 demo 模式：

```bash
python3 -m insight_capture.runtime.app --fake-pose
```

浏览器打开 `http://localhost:8765/`，能看到几个 pose 节点随假数据运动。

容器内如果还想在本机同时拉起右侧 3D 窗口（不通过 `run_dashboard.sh` 的话）：

```bash
python3 -m insight_capture.runtime.app &
./deploy/kiosk/open_web_3d_right.sh
```

## 单一配置入口：config/cameras.json

> `config/cameras.json`、`config/runtime.json`
> 是按设备生成的产物（`.gitignore` 掉了），源头是 `config/devices/<name>/`，用
> `scripts/select_device.sh <name>` 切换/生成——直接改 `config/` 下的文件本身没问题
> （对当前选中的设备生效），但换设备/长期改动要改回 `config/devices/<name>/` 里的源文件，
> 否则下次 `select_device.sh` 会把改动覆盖掉。详见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

**优先只改当前机器的 `config/cameras.json`**。它控制：

- dashboard 显示哪几路图像、用哪几路 VIO
- 默认 `ROS_DOMAIN_ID`
- jetson-nx 相机使用的 `camera_dds_type`，以及固件只声明但不发数据的
  `recording_excluded_topics`

每个 camera 条目的常用字段：

| 字段 | 作用 | 例子 |
|---|---|---|
| `namespace` | 相机的 ROS 命名空间 | `insight3_a` |
| `dashboard_image_stream` | dashboard 显示用哪路图像流 | `infra1` / `color_compressed` |
| `dashboard_pose_stream` | 全局建图/重定位 pose 流 | `/insight_global/insight3_a/pose` |
| `teleop_role` | 决定 3D 场景里用哪个位置/朝向预设 | `head` / `left_hand` / `right_hand` |

`dashboard_image_stream` 的可选值定义在
[config.py](insight_capture/core/config.py) 的 `IMAGE_STREAMS` 里：`infra1`、
`infra2`、`depth`、`color`、`color_compressed`。

### 当前命名约定

按 `config/cameras.json` 里实际配置的机队为准，改命名空间/角色直接改这个文件，不需要碰代码。

## 相机重启与网络

```bash
./scripts/reboot_cameras.sh
```

会自动扫描当前活跃的 `169.254.x.x` 网络接口发现相机（每台相机独占一个 USB 网口、独立 `/24` 段），不需要在脚本里写死 IP；换了 segment 或增减相机都不用改脚本。

三台相机和 Dashboard 必须使用同一个、且同一局域网内不冲突的 ROS Domain。统一修改时执行：

```bash
./scripts/set_ros_domain_id.py 21 --dry-run
./scripts/set_ros_domain_id.py 21
```

脚本会先确认三台相机均可访问且当前没有录制，再同时维护相机设置、
`config/cameras.json` 和 Compose `.env`。默认随后重启三台相机并强制重建三个 ROS
容器，使新 ID 真正生效；自动化环境可加 `-y`，只写配置暂不重启可加
`--no-restart`。`config/cameras.json` 是当前机器的 live 配置，之后重新执行
`select_device.sh` 会用 profile 默认值覆盖它，需要时应再次运行本脚本。

需要在整机重启后三机统一校时、共同重启采集服务并直接测量图像时间戳差时：

```bash
./scripts/reboot_cameras.sh --sync-phase
```

该模式使用 Insight3 A/B 红外图像与 Insight9 中间 30 Hz RGB 图像进行测量；SSH
密码只交互读取一次，也可用 `INSIGHT_CAMERA_SSH_PASSWORD` 或 SSH key 提供。

## Web Dashboard 功能

启动后同时提供：

- WebSocket: `ws://<host>:8765/ws`
- pose 快照: 随 WebSocket 首帧推送（不是独立的 `/api/poses` GET）
- recording 状态/topics: `http://<host>:8765/api/recording/status`、`/api/recording/topics`
- rosbag 列表: `http://<host>:8765/api/rosbags`
- 健康检查: `http://<host>:8765/healthz`

页面入口：

- `/`：Session / Take 采后列表、quick QC、人工决定与异常摘要
- `/3d`：三路画面与全局建图/重定位轨迹的 Babylon.js GPU 场景，viewer 打开后才启动媒体链路
- `/recording`：维护用 rosbag 录制页；现场主流程使用离线语音
- `/bags`：本地 rosbag 列表页，大小/时长/消息/topic 数、完整性与评分状态
- `/umi-dataset`：LeRobot v3 标准数据集与 Legacy UMI Zarr 导出
- `/scoring`：录制完整性验证与轨迹评分
- `/handpose`：从已有 rosbag 离线提取并查看 WiLoR 3D 手部关键点
- `/optimization`：COLMAP 轨迹优化（`jetson-nx` 镜像内置 CUDA sm_87 的 COLMAP 3.9.1）
- `/settings`：夹爪/手部叠加与 Insight3 mask 等高级设置

Recording 页面：`Refresh Topics` 按当前 `ROS_DOMAIN_ID` 发现 live topic（按相机分组，
支持整组勾选），`Start` 只录勾选的 topic。全部勾选消息由一个原生 C++
`ros2 bag record --storage mcap` 进程使用 CycloneDDS 直接写成标准单目录 rosbag。recorder
先暂停等待三台相机订阅就绪，再从统一边界开始；Dashboard 会在 resume 后补发缓存的
`/tf_static`。dashboard 图像 callback 只额外做 header 连续性审计，不参与序列化或磁盘写入。
`Stop` 等待 writer cache 排空后将
staging 目录原子发布，同时保存 live header/network audit；不分 part、不合包、不重写 payload，
发布后即可开始下一段。默认选择同时包含原始
`vio_100hz` 和配置的全局 pose，供单臂/双臂数据集导出使用。
录制空闲时可在页面的 **Recording folder → Browse...** 中选择后端已挂载的可写目录；
选择前会执行一次写入/`fsync` 探测，录制中或停止排空期间不能切换。网页不会枚举整台设备，
也不能选择操作者电脑上未挂载进 Dashboard 容器的目录。切换只改变后续录制的写入目标；
`/bags` 可聚合已配置录制根目录中的历史数据，并在“全部目录”和“当前录制目录”之间筛选。

输出目录优先级：CLI `--rosbag-dir` >
环境变量 `INSIGHT_ROSBAG_DIR` > `config/runtime.json` > 默认 `rosbags`。
Docker 宿主机目录由 `INSIGHT_ROSBAG_HOST_DIR` 控制；例如在 `.env` 写入
`INSIGHT_ROSBAG_HOST_DIR=/media/nvidia/INSIGHT_USB/rosbags`，即可将容器录制根目录
直接绑定到 ext4 U 盘。还可设置 `INSIGHT_ROSBAG_REQUIRED_SOURCE=/dev/sda1`；后端启动时及
每次开始录制前都会核对挂载源，并在 `_staging` 做一次写入/`fsync` 探测。挂载不匹配或
探测失败时自动回退到本机 NVMe 的 `rosbags/`，本进程随后固定使用 NVMe，避免 U 盘抖动
导致录制根目录来回切换。可写且空间充足的 fallback 只产生 warning，不会阻止录制或将
Take 标成 suspect；目录不可写或空间不足仍会拒绝开始。录制状态的 `storage.active_path`、
`storage.using_fallback` 和 `storage.fallback_reason` 会显示实际写入位置与回退原因。

声控统一使用宿主机上的 [Insight 离线语音控制](docs/OPENCLAW_VOICE.md)。SenseVoice、
VAD 与 Piper 在本地完成“开始任务叠杯子、当前任务多少条、结束当前任务、开始/停止录制、
录制校准模式、开始校准、检查相机、系统状态、本条作废”等
固定命令；开始前自动执行 Preflight，不满足时拒绝并播报原因。OpenClaw 只处理唤醒词
后的非固定自然语言，未安装或断网不影响固定命令。

Bags 列表页扫描 `metadata.yaml`，展示递归文件大小、duration、message/topic 数量，
并从 `outputs/results/{integrity,scores}` 读取完整性与评分状态。

## 入口与实现位置

| 入口/模块 | 作用 |
|---|---|
| `scripts/run_dashboard.sh` | 统一启动入口，`docker compose up -d` + 健康检查，`--jetson` 额外拉起本机 kiosk 窗口 |
| `scripts/run_voice.sh` | 离线固定命令、本地 STT/TTS 与可选 OpenClaw adapter |
| `insight_capture.runtime.app` | Web dashboard 稳定进程入口与 ROS 生命周期组合 |
| `insight_capture/runtime/` / `api/` | 现场运行时、HTTP API 与 WebSocket 实现 |
| `insight_capture/media/` | viewer 按需启用的 JPEG、preview 与 WebRTC |
| `deploy/kiosk/open_web_3d_right.sh` | 本机拉起指向 Web 3D 页面的全屏浏览器 kiosk |
| `insight_capture/postprocess/` | 完整性、评分、回放、同步、WiLoR、LeRobot 与优化实现 |
| `insight_capture/runtime/mapping/` | Insight9 sparse mapping、Insight3 localization 与 SuperGlue |
| `tools/mapping_validation/` | Dense Mapping 与 RViz 工程验证入口 |
| `insight_capture/legacy/` | 历史 SQLite/composite bag 与 UMI Zarr 读取兼容 |
| `Dockerfile.superglue-validation` | 客户发布与开发共用的 NVIDIA Jetson TensorRT/SuperGlue GPU 镜像 |
| `insight_capture/core/config.py` | 从 `config/cameras.json` 生成 dashboard 所需 topic |
| `scripts/reboot_cameras.sh` | 扫描 `169.254.x.x` 网段并批量重启相机 |
| `scripts/sync_camera_restart.py` | 三相机共同定时重启采集服务并测量图像时间戳差 |
| `insight_capture/perception/gripper/` | 在线/离线共用的夹爪识别、手部叠加与标定 |
| `insight_capture/postprocess/gripper/` | rosbag 夹爪离线提取与旧 import 兼容入口 |
| `insight_capture.postprocess.datasets.lerobot` / `insight_capture.legacy.umi_zarr` | LeRobot v3 标准归档与 Legacy UMI 数据集导出 |
| `insight_capture.postprocess.datasets.ego_lerobot.cli` / `insight_capture/postprocess/datasets/ego_lerobot/` | 三视角、仅头部手姿的缓存式 Ego LeRobot 交付流水线 |
| `insight_capture.postprocess.quality.trajectory_score` | 对一份 rosbag 做轨迹评分（命令行工具，`--help` 看参数） |
| `web_dashboard/` | Babylon.js Web 前端源码，`npm run build` 生成 `dist/` 静态页面 |
| `config/runtime.json` | 录制、Preflight、Session/Take 和主动 QC 配置 |
| `config/capture_tasks.json` | 内置 Task 名称、导出指令、采集 profile 与默认任务；前端新增任务持久化到 `outputs/results/sessions/<task_id>/task.json` |

新增功能和迁移规则见 [项目结构规范](docs/PROJECT_STRUCTURE.md)。

## 部署

打包发布镜像、给设备升级、全新设备首次部署的完整步骤见
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**。这里只记设备差异的技术背景：

### 设备与 config profile

三台设备（`jetson-nx`/`lite`/`lite-779`）现在共用同一个 `main` 分支，不再各自维护
一个 git 分支——设备差异收敛到 `config/devices/<name>/` 下的两个文件（见上方"单一
配置入口"一节），用 `scripts/select_device.sh <name>` 选择。只有 `jetson-nx` 会打
正式发布镜像，`lite`/`lite-779` 是开发机专用 profile。

### COLMAP：只在 jetson-nx profile 上开箱即用

COLMAP（3.9.1，CUDA sm_87，GUI 关闭）和 `looper-vio-colmap-handoff` 流水线在
`docker compose build` 时编译/克隆进镜像（见 Dockerfile 的 `colmap-builder`
阶段），不需要任何宿主机挂载或每台设备手工编译——`/optimization` 开箱即用。
只支持 Orin NX（sm_87 单架构编译），不支持 Nano，因此这也是目前唯一会打正式
发布镜像的设备。`lite`/`lite-779` 这两台开发机没有 COLMAP 依赖，`/optimization`
页面不可用。

`looper-vio-colmap-handoff` 钉在固定 commit 上，其上打了两个本地补丁
（COLMAP 3.9.1 的 GPU 参数名、stdbuf 行缓冲让网页日志实时刷新），升级
该仓库 commit 时需要复核补丁是否仍然适用（见 Dockerfile 内注释）。

部署到新机器后，`config/cameras.json`、`config/runtime.json`
都是跟**当前这批相机/这台设备**绑定的配置（均由 `select_device.sh`
从 `config/devices/<name>/` 生成，不是手改的）。换了相机后需要更新命名空间、
图像流和全局重定位 topic，不能直接沿用另一台设备的配置。
