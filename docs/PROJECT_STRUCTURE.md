# 项目结构

仓库按产品运行阶段划分：`runtime` 只负责现场采集，`voice` 是无屏操作界面，
`postprocess` 和 `web_dashboard` 服务于采后查看、质检及导出。`scripts` 只保留
部署命令、运维命令和兼容薄入口，不能再承载业务实现。

```text
insight_capture/
  runtime/                 现场录制、Preflight、Session/Take、主动 QC、建图定位
    recording/             MCAP recorder、存储、header audit、恢复
    mapping/               Insight9、Insight3、SuperGlue、位姿图和重定位
  voice/                   离线固定命令、ALSA、SenseVoice/VAD、Piper、可选 OpenClaw
  media/                   viewer 出现后才工作的 JPEG、preview、WebRTC worker
  web/                     aiohttp app、context、WebSocket 和领域 routes
  postprocess/
    bags/                  catalog、完整性、回放、同步
    quality/               轨迹评分和 station check
    handpose/              WiLoR、滤波和 schema
    gripper/               tracking、calibration、extraction
    datasets/              routing、LeRobot、Ego LeRobot、UMI 兼容 facade
    optimization/          COLMAP
  common/                  models、paths、config
  legacy/                  SQLite/composite bag 与 UMI Zarr 兼容实现

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

依赖方向固定为“入口 → 领域包 → 小型 adapter”。Web route 可以调用 manager，
manager 不反向导入 route；worker 可以调用媒体或感知实现，实现模块不启动 worker。

## 稳定入口与兼容面

- `scripts/` 固定只保留上面列出的七个薄入口；业务实现不得放回该目录。
- Dashboard、语音、后处理和工程工具直接通过 `python3 -m insight_capture...` 或
  `tools/...` 调用，不再保留同名 Python facade。
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
