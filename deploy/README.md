# Insight Dashboard 部署与升级说明

本目录（部署包）安装一次即可；之后每次软件升级只需要一个镜像压缩包
`insight-dashboard-vX.Y.Z.tar.gz`，用 `update.sh` 加载并重启即可，
数据（录制的 rosbag、标定、配置）全部保留。

## 环境要求

- NVIDIA Jetson（JetPack 6.x，已装 Docker 与 nvidia-container-runtime）
- 当前用户能执行 `docker`（在 `docker` 用户组里）

## 首次安装

把部署包和镜像压缩包拷到设备上（U 盘 / scp 均可）：

```bash
tar xzf insight-dashboard-deploy-vX.Y.Z.tar.gz
cd insight-dashboard-deploy        # 解压出的目录不带版本号——它是常驻安装目录，名字跨版本不变
./update.sh /path/to/insight-dashboard-vX.Y.Z.tar.gz
sudo ./scripts/host_setup.sh       # 一次性调优：CycloneDDS/UDP 分片、RPS + 开机相机恢复
```

`update.sh` 会加载镜像、生成 `config/` 等数据目录并启动服务，
最后等待后端健康检查通过。`host_setup.sh` 只在首次安装（或重刷系统后）需要跑一次；
jetson-nx profile 会在下一次开机相机恢复流程中把相机 DDS 模式校正为 CycloneDDS。

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

浏览器访问：`http://<设备IP>:8765/`

## 升级

拿到新的镜像压缩包后，在本目录执行：

```bash
./update.sh insight-dashboard-vX.Y.Z.tar.gz
```

- 正在录制时会拒绝升级（避免打断录制）；确认要打断就加 `--force`。
- `config/`、`rosbags/`、`outputs/`、`runs/` 都在宿主机上，升级不受影响。

## 回滚

旧版本镜像加载过就还在本机，一条命令切回去：

```bash
./update.sh --rollback v1.1.0
```

查看本机已有的版本：`docker image ls insight-dashboard`。
磁盘紧张时用 `docker rmi insight-dashboard:<旧版本>` 清理不再需要的版本。
建图/重定位用的 `insight-superglue-validation` 镜像不跟着应用版本号走，回滚
不会影响它，也不需要单独处理。

## 目录说明

| 路径                 | 内容                                   |
| -------------------- | -------------------------------------- |
| `.env`               | 当前运行的版本号（`update.sh` 维护）   |
| `config/`            | 相机/标定/后处理配置（升级保留）       |
| `rosbags/`           | 录制数据（升级保留）                   |
| `outputs/`, `runs/`  | 处理结果（升级保留）                   |
