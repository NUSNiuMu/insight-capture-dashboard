# Insight Dashboard 部署与升级说明

本目录（部署包）安装一次即可；之后每次软件升级只需要一个镜像压缩包
`insight-dashboard-vX.Y.Z.tar.gz`，用 `update.sh` 加载并重启即可，
数据（录制的 rosbag、标定、配置）全部保留。

自 v2.0.0 起，客户部署同时包含 SuperGlue 推理、Insight9 稀疏建图和
Insight3 全局重定位服务；稠密建图和 RViz 验证服务不进入客户发布包。

## 环境要求

- NVIDIA Jetson（JetPack 6.x，已装 Docker 与 nvidia-container-runtime）
- 当前用户能执行 `docker`（在 `docker` 用户组里）

## 首次安装

把部署包、Dashboard 镜像和首次安装依赖包拷到设备上（U 盘 / scp 均可）。
两个镜像压缩包放在同一目录；稳定的 SuperGlue 依赖只在首次安装时传一次：

```bash
tar xzf insight-dashboard-deploy-vX.Y.Z.tar.gz
cd insight-dashboard-deploy        # 解压出的目录不带版本号——它是常驻安装目录，名字跨版本不变
./update.sh /path/to/insight-dashboard-vX.Y.Z.tar.gz
sudo ./deploy/host_setup.sh       # 一次性调优：CycloneDDS/UDP 分片、RPS + 开机相机恢复
```

`update.sh` 会加载 Dashboard 镜像；如果本机还没有
`insight-superglue-validation:25.04`，会自动从 Dashboard 镜像旁边的
`insight-superglue-validation-25.04.tar.gz` 加载。随后生成 `config/` 等数据目录并启动服务，
最后等待后端健康检查通过。`host_setup.sh` 只在首次安装（或重刷系统后）需要跑一次；
jetson-nx profile 会在下一次开机相机恢复流程中把相机 DDS 模式校正为 CycloneDDS。
安装的 `insight-camera-network.service` 负责网卡参数，
`insight-camera-reboot.service` 在冷启动后恢复相机并校验 DDS 配置。

`update.sh` 会从当前 Dashboard 镜像同步宿主机语音代码，并在离线模型已经准备好时自动安装、
启用或升级 `insight-voice-control.service`；若发现旧的
`looper-openclaw-voice.service`，会自动停止并迁移。语音模型尚未准备好的设备会明确提示并
跳过，不影响 Dashboard 启动；补齐模型后可手动执行
`./deploy/install_voice_control_service.sh`。

语音服务作为发布包默认组件运行在宿主机，直接使用宿主机声卡；Dashboard 容器不再
挂载 `/dev/snd`。安装脚本默认自动发现稳定的 ALSA `CARD` 名称，不依赖 card index。
SenseVoice、Silero VAD 和 Piper 固定命令完全离线；没有安装 OpenClaw 或 Gateway
不可用时，只会禁用非固定自然语言问答，不影响开始/停止录制、校准、相机检查、
系统状态和本条作废。

手动整机重启三台相机后，如需统一校时、共同重启采集服务并直接测量三路图像
header timestamp 差，可在部署目录执行：

```bash
./scripts/reboot_cameras.sh --sync-phase
```

该模式交互式读取一次相机 SSH 密码，也支持 `INSIGHT_CAMERA_SSH_PASSWORD` 或
`INSIGHT_CAMERA_SSH_IDENTITY`。开机自动流程的无人值守配置见部署包内
`tools/device_cli/README_cn.md`；宿主机缺少 Paramiko 时先执行
`sudo apt-get install python3-paramiko`。

首次安装或大版本升级后，建图/重定位用的 TensorRT 推理服务需要现场编译一次
设备专属引擎，最长约 15 分钟；这段时间看板本身已经能正常打开，只是 3D 页面的
全局轨迹要等引擎编译完才出现，是正常现象，不是卡住。之后的引擎缓存会保留在
本机，重启和常规升级不会重新触发这次编译。

## 日常使用

```bash
./scripts/run_dashboard.sh            # 启动/确认后端在跑，Ctrl-C 停止
./scripts/run_dashboard.sh --jetson   # 同时在本机屏幕打开看板窗口
docker compose logs -f                # 查看后端日志
docker compose down                   # 停止
```

浏览器访问：`http://<设备IP>:8765/`。可用页面包括 3D、录制、Bags、
LeRobot/UMI 数据集、轨迹评分、Hand pose、轨迹优化和设置。

## 升级

拿到新的镜像压缩包后，在本目录执行：

```bash
./update.sh insight-dashboard-vX.Y.Z.tar.gz
```

- 正在录制时会拒绝升级（避免打断录制）；确认要打断就加 `--force`。
- `config/`、`rosbags/`、`outputs/`、`runs/` 都在宿主机上，升级不受影响。
- 要直接写 ext4 U 盘，在 `.env` 增加
  `INSIGHT_ROSBAG_HOST_DIR=/media/nvidia/INSIGHT_USB/rosbags` 并重新执行
  `docker compose up -d insight-dashboard`；建议同时设置
  `INSIGHT_ROSBAG_REQUIRED_SOURCE=/dev/sda1`。服务启动及每次开始录制前会验证挂载源和
  `_staging` 写入，U 盘未挂载、只读或 I/O 异常时自动回退到本机 NVMe 的 `rosbags/`；
  录制状态会显示实际路径与回退原因。一次回退后需重启 Dashboard 才会重新选择 U 盘；
  `update.sh` 升级时会保留这些设置。

## 回滚

旧版本镜像加载过就还在本机，一条命令切回去：

```bash
./update.sh --rollback v2.0.3
```

查看本机已有的版本：`docker image ls insight-dashboard`。
磁盘紧张时用 `docker rmi insight-dashboard:<旧版本>` 清理不再需要的版本。
建图/重定位用的 `insight-superglue-validation` 镜像不跟着应用版本号走，回滚
不会影响它，也不需要单独处理。

## 目录说明

| 路径                 | 内容                                   |
| -------------------- | -------------------------------------- |
| `.env`               | 当前版本号及本机录制目录覆盖（升级保留） |
| `config/`            | 相机/标定/后处理配置（升级保留）       |
| `rosbags/`           | 录制数据（升级保留）                   |
| `outputs/`, `runs/`  | 处理结果（升级保留）                   |

更完整的首次部署、发版与故障恢复流程见 [部署手册](../docs/DEPLOYMENT.md)。
