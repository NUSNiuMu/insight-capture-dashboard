# 项目结构

仓库采用模块化单体：`runtime` 负责现场采集，`services` 负责任务编排，
`api` 只负责 HTTP/WebSocket 适配，`perception` 和 `quality` 是在线与离线流程
都可复用的领域能力。`postprocess` 只处理已有数据，`scripts` 只保留部署命令、
运维命令和兼容薄入口，不能再承载业务实现。

```text
insight_capture/
  core/                    最底层 models、paths、config、性能工具
  runtime/                 现场录制、Preflight、Session/Take、主动 QC、建图定位
    app.py                 仅负责配置、服务组装和进程生命周期
    bootstrap.py           路径解析、崩溃诊断和 recorder factory
    ros/                   PoseBridgeNode、ROS QoS、订阅与 adapter 协调
    recording/             MCAP recorder、存储、header audit、恢复
    mapping/               Insight9、Insight3、SuperGlue、位姿图和重定位
  voice/                   离线固定命令、ALSA、SenseVoice/VAD、Piper、可选 OpenClaw
  perception/
    gripper/               在线/离线共用的 marker tracking、hand overlay、calibration
  quality/                 在线与采后共用的 capture gate
  media/                   viewer 出现后才工作的 JPEG、preview、WebRTC worker
  services/                dataset export、scoring、gripper extraction 任务编排
  api/                     aiohttp app、context、WebSocket 和薄 routes
  postprocess/
    bags/                  catalog、完整性、回放、同步
    quality/               采后轨迹评分
    handpose/              WiLoR、滤波和 schema
    gripper/               离线 extraction；旧 tracking/calibration 路径为兼容 wrapper
    datasets/              routing、LeRobot、Ego LeRobot、UMI 兼容 facade
    optimization/          COLMAP
  legacy/                  SQLite/composite bag 与 UMI Zarr 兼容实现
  common/ web/             旧 import 路径的薄兼容 namespace

web_dashboard/src/
  sessions/ review/ datasets/ storage/
  advanced/{handpose,trajectory,optimization,system}/
  shared/

scripts/
  run_dashboard.sh run_voice.sh select_device.sh
  reboot_cameras.sh sync_camera_restart.py
  check_bag.py export_lerobot.py

tools/
  device_cli/ mapping_validation/ diagnostics/

deploy/
  docker-compose.yml update.sh systemd/ kiosk/
  build_release.sh setup_host.sh host_setup.sh

tests/
  runtime/ voice/ postprocess/ web/ legacy/
```

依赖方向固定为“入口/API → services/runtime → 领域包 → core”。API route 可以调用
service，service 不反向导入 route；runtime 与 postprocess 都可依赖 perception/quality，
但 runtime 不得反向依赖 postprocess；worker 可以调用媒体或感知实现，实现模块不启动
worker。结构测试会拒绝生产代码重新导入旧 `common/web` 路径或旧的
`postprocess.gripper` 感知实现路径。

根目录 `pyproject.toml` 统一声明项目 metadata、Python 依赖、console scripts 以及
pytest/ruff/mypy/pyright 配置。当前阶段保留根目录 package 布局，待入口和部署全部通过
安装后的 console scripts 验证后，再单独迁移到 `src/insight_capture/`。

## 稳定入口与兼容面

- `scripts/` 固定只保留上面列出的七个薄入口；业务实现不得放回该目录。
- Dashboard、语音、后处理和工程工具可通过 `pyproject.toml` 声明的
  `insight-*` console scripts、`python3 -m insight_capture...` 或 `tools/...`
  调用；旧 package 路径只保留无业务逻辑的兼容 wrapper。
- 旧 SQLite/composite bag 和 UMI Zarr 仍需读取历史数据，因此集中在 `legacy/`，
  不能在没有数据迁移策略时删除。
- `config/runtime.json` 是新 live 配置；启动代码临时接受旧
  `config/post_processing.json`，用于不切换 profile 的现场机器平滑升级。

## 运行边界

- Capture Mode 无 viewer 时不做 JPEG preview、WebRTC 传输或手部显示叠加；录制、
  header audit、2 Hz localization relay、VIO/mapping/QC 持续运行。
- viewer lease 到来后按需启动媒体 worker，最后一个 viewer 离开并超过 idle grace
  后停止。
- 固定语音命令完全离线执行；OpenClaw adapter 只处理非固定自然语言。
- Dense Mapping、RViz、COLMAP 位于 Engineering/Advanced 路径，不进入日常采集主流程。
- 3D review 保留真实相机模型、三路 camera frame、pose、trajectory、TCP 和 hand pose。
