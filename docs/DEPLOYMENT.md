# 部署与发布手册

本仓库的镜像化部署/升级流程涉及三类人、三个入口脚本：

| 谁 | 做什么 | 用哪个脚本 |
|---|---|---|
| 开发者 | 把当前分支的代码编译成一个可分发的镜像包 | `scripts/build_release.sh` |
| 使用者 | 拿到镜像包，给已经在跑的设备升级/回滚 | `deploy/update.sh` |
| 任何人 | 全新 Jetson 首次部署 | `scripts/setup_host.sh`（开发者路径）或 `deploy/update.sh`（使用者路径，见下） |

三者的关系：`build_release.sh` 在开发机上跑，产出 Dashboard 镜像、稳定的 SuperGlue
依赖镜像和部署包三个压缩文件。后两者只在**首次安装**时需要；之后每次升级只需要发
一个新的 Dashboard 镜像，使用者已有的 `update.sh` 和 SuperGlue 镜像不变。

### 设备与 config profile

三台设备（`jetson-nx`/`lite`/`lite-779`）现在共用同一个 `main` 分支，不再各自一个 git
分支。设备差异收敛到 `config/devices/<name>/{cameras.json,post_processing.json}`
两个文件，`scripts/select_device.sh <name>` 把选中的 profile
复制成 `config/` 下真正生效的文件（这两个文件本身是 `.gitignore` 掉的本地生成产物）。
只有 `jetson-nx` 会走 §1 打成正式发布镜像；`lite`/`lite-779` 是开发机专用 profile。

---

## 1. 开发者手册：怎么编译发布镜像

### 1.1 前提

- 在 Jetson（arm64）上编译，不能在 x86 开发机上交叉编译当前的 Dockerfile
  （COLMAP 的 CUDA sm_87 编译在 `colmap-builder` 阶段，依赖宿主机的 NVIDIA 工具链）。
- 当前 `main` 就是要发布的版本——`build_release.sh` 直接 `docker build` 工作目录，
  发布前先确认 `git status` 干净、该合的改动都合并了。
- `config/` 必须选中 `jetson-nx` profile（`scripts/select_device.sh jetson-nx`）——
  三台设备现在共用同一个分支，`config/cameras.json` 等两个文件是本地生成产物，
  见"分支与部署"一节；`build_release.sh` 会自己校验这一点，选错了会直接报错退出。
- 本地先用根目录的开发机 `docker-compose.yml`（`docker compose build && docker compose up -d`）
  把要发布的改动完整跑一遍，并用无头浏览器或人工逐页检查八个页面，
  **不要用没跑过的代码直接打包发布**。

### 1.2 构建缓存与镜像体积

Dashboard 镜像包含离线 WiLoR 所需的固定版本权重、JetPack PyTorch 和 CUDA
运行库。首次或缓存失效时需要下载、校验并导出这些数 GB 内容，耗时可能达到
数十分钟；这不是每次启动都需要做的工作。Dockerfile 把固定权重和运行时放在
源码 `COPY` 之前，普通源码变更会复用这些层。开发 compose 又把仓库 bind mount
到容器内，因此日常 Python/前端修改通常执行
`docker compose up -d insight-dashboard`（必要时加 `--force-recreate`）即可。
只有 Dockerfile、系统包或 Python 依赖发生变化时才重新 build。

WiLoR 层只保留离线推理路径：不包含 CUDA 编译器/头文件、WiLoR 训练和 demo
依赖，也不允许运行时自动下载模型或补装 Python 包。上游 2.56GB FP32 checkpoint
会在构建时转换为约 1.28GB FP16 推理权重；Jetson GPU 路径原本就在加载前把模型
转换为 FP16，因此这不会增加运行时量化步骤，同时降低镜像体积和模型加载内存。

Dockerfile 自 `v2.0.2` 起拆成 `runtime`/`dev` 两个 target：`dev`（`runtime`
基础上追加 Playwright headless Chromium，仅用于 CLAUDE.md 里的无头页面验证）
是根目录开发 compose 的默认构建目标；`build_release.sh` 显式用
`--target runtime`，客户镜像不含这层调试用的 Chromium，体积因此减少约 1.8GB
（未压缩）。这两个 target 共享除 Chromium 外的全部层，互不影响构建缓存。

### 1.3 打包

```bash
./scripts/build_release.sh v2.0.5
```

版本号必须形如 `vX.Y.Z`（可带 `-rc1` 之类后缀），脚本会校验格式。产出：

```
release/insight-dashboard-v2.0.5.tar.gz          # 镜像；每次升级都发这个
release/insight-superglue-validation-25.04.tar.gz # 稳定依赖；仅首次安装发送
release/insight-dashboard-deploy-v2.0.5.tar.gz   # 部署包；只有首次安装的设备需要
```

镜像 tar 通常几 GB（含 COLMAP、CUDA 运行时、Firefox），传输/拷 U 盘预留时间。
自 v2.0.0 起，`build_release.sh` 会额外构建并保存
`insight-superglue-validation:25.04`（Insight9 建图/Insight3 全局重定位依赖的
Magic Leap 官方 SuperPoint/SuperGlue TensorRT 推理，商用分发已确认，见
`docs/INSIGHT9_SPARSE_MAPPING.md`）。它现在单独保存为稳定依赖包，避免每个
Dashboard 日常升级包都重复携带同一份 TensorRT/CUDA 内容。
部署包里打包了 `deploy/docker-compose.yml`、`update.sh`、`README.md`、
`scripts/run_dashboard.sh`，以及宿主机一次性调优用的 `scripts/host_setup.sh`
+ `scripts/configure_camera_network.sh` + `scripts/reboot_cameras.sh`
+ `scripts/sync_camera_restart.py`
+ `scripts/systemd/{insight-camera-network,insight-camera-reboot}.service`
+ `looper_cli/`。这些文件本身不大，但**它们的内容来自当前
分支的 `deploy/`、`scripts/`、`looper_cli/` 目录**，改过 `deploy/docker-compose.yml`
（比如调 shm_size）或 `scripts/host_setup.sh` 一定要在改动落地之后的这个分支上
重新跑一次 `build_release.sh`，不能沿用旧的部署包。

### 1.4 版本号约定

正式版本已从 `v2.0.0` 起使用与镜像版本一致的 git tag（当前最新发布 tag 为
`v2.0.4`）。后续仍须让镜像版本号对应同名 tag，例如
`git tag v2.0.5 && git push origin v2.0.5`，方便从设备版本反查代码状态；
`build_release.sh` 本身不会自动打 tag。

### 1.5 发布前检查清单

- [ ] 本地开发机 compose 跑过一遍，八个页面（3d / recording / bags / umi-dataset / scoring / handpose / optimization / settings）无 console 报错
- [ ] `superglue-inference` 健康检查能通过（本地至少验证一次冷启动 TensorRT 引擎编译），
  `insight9-sparse-mapper`/`insight3-global-localizer` 正常起来，3d 页面能看到全局轨迹
- [ ] 涉及数据库结构/配置字段变化的改动，确认旧版本升级上来后不会因为缺字段崩溃
  （`config/` 目录在首次安装后不会被镜像覆盖，见下文"数据持久化"）
- [ ] `git status` 干净，且要发布的 commit 已经推到远端（发布包本身不含 git 历史，事后排查靠 commit hash）
- [ ] 如果这次发布改了 `Dockerfile`（新依赖），本地至少验证过 `docker compose build` 全新构建成功一次（不是吃的旧层缓存）

### 1.6 交付

- **首次安装**：把 Dashboard 镜像、SuperGlue 依赖和部署包三个压缩文件都发给对方。
- **升级**：只发 Dashboard 镜像，对方在已有的部署目录里跑 `./update.sh <镜像tar>`。

---

## 2. 使用者手册：怎么用镜像升级本地项目

面向已经完成过一次首次安装、日常在用这套系统的设备。等价内容也在
`deploy/README.md`（部署包里自带的版本）和 `docs/USAGE.md` §5，这里给更完整的排查步骤。

### 2.1 日常升级

```bash
cd <部署目录>   # 首次安装时 update.sh 所在的那个目录
./update.sh insight-dashboard-vX.Y.Z.tar.gz
```

这一条命令做的事：

1. `docker load` 镜像 tar；
2. 把新版本号写进 `.env`（`INSIGHT_VERSION=vX.Y.Z`）；
3. `docker compose up -d`（用 `deploy/docker-compose.yml`，只挂数据目录，不挂源码）；
4. 轮询 `http://localhost:8765/healthz` 最长 90 秒，成功打印完成信息，超时会报错并提示怎么回滚。

### 2.2 安全机制

- **录制中拒绝升级**：`update.sh` 会先查 `/api/recording/status`，正在录制时直接报错退出，
  避免打断一次拍摄；确认要打断就加 `--force`。
- **数据持久化**：`config/`、`rosbags/`、`outputs/`、`runs/` 都在宿主机上（不在镜像里），
  升级只换代码，不动这些目录。`config/` 只在**首次安装**（该目录不存在时）从镜像里播种一次，
  之后永远不会被镜像内容覆盖——本机的标定、机群配置都不会因为升级而丢失或被重置。
- **ext4 U 盘直录**：在部署目录的 `.env` 增加
  `INSIGHT_ROSBAG_HOST_DIR=/media/nvidia/INSIGHT_USB/rosbags`，确认该目录已挂载且可写后执行
  `docker compose up -d insight-dashboard`。Compose 会把它绑定为容器录制根目录；
  同时设置 `INSIGHT_ROSBAG_REQUIRED_SOURCE=/dev/sda1`。服务启动时及每次开始录制前会核对
  挂载源，并在 `_staging` 执行写入/`fsync` 探测；挂载不匹配或出现 I/O 错误时自动回退到
  本机 NVMe 的 `rosbags/`，本进程不再自动切回 U 盘。API 状态会报告实际路径、
  `storage.using_fallback` 和 `storage.fallback_reason`；`update.sh` 更新版本号时会保留这些
  本机设置。
- **旧版本不会被自动清理**：`docker load` 过的镜像会一直留着，方便回滚；磁盘紧张时自己
  `docker image ls insight-dashboard` 查、`docker rmi insight-dashboard:<旧版本>` 清理不需要的。

### 2.3 回滚

```bash
./update.sh --rollback v2.0.3
```

要求 `v2.0.3` 这个镜像之前 `docker load` 过（还在本机）。等价于把 `.env` 指回旧版本号
再 `docker compose up -d`，同样过一遍 healthz 检查。

### 2.4 升级失败排查

| 现象 | 大概率原因 | 怎么查 |
|---|---|---|
| `docker load` 报错 / 卡住 | 镜像 tar 传输不完整 | 检查文件大小、`md5sum`/`sha256sum` 跟发布方核对 |
| healthz 90 秒超时 | 后端容器起不来 | `docker compose logs -f`；常见于新版本引入的依赖没装全（对照 §1.5 发布前检查） |
| 升级后配置/标定"消失" | 罕见——config/ 首次安装后不该被覆盖 | 确认没有手动删过 `config/` 目录；`ls config/` 核对文件还在 |
| "a recording is in progress" | 有正在跑的录制 | 等它录完，或确认可以中断后加 `--force` |
| 宸境声控无响应 | 宿主机语音服务未运行或 USB 声卡未枚举 | 检查 `systemctl --user status looper-openclaw-voice.service`、`arecord -l` 和该服务日志 |

---

## 3. 全新 Jetson NX：怎么从零部署这套项目

两条路径，二选一：

### 3.1 开发者路径（有源码访问权限，要在这台设备上继续开发）

```bash
git clone git@github.com:NUSNiuMu/insight-capture-dashboard.git insight_capture
cd insight_capture
./scripts/setup_host.sh --device jetson-nx
```

`main` 现在是唯一的代码分支，三台设备（`jetson-nx`/`lite`/`lite-779`）的差异只体现在
`config/devices/<name>/{cameras.json,post_processing.json}`——
`--device <name>` 会先跑一次 `scripts/select_device.sh <name>`，把对应 profile 复制成
`config/` 下真正生效的文件（已经选过的话这个参数可以省略）。

`setup_host.sh` 之后是幂等的一次性宿主机配置，按顺序做：

1. 选定/校验设备 profile（见上）；
2. 检查 docker + NVIDIA container runtime 是否就绪（硬件 JPEG/H.264 编解码依赖它注入 GStreamer 插件）；
3. 调用 `scripts/host_setup.sh`（与使用者路径共用，见 §3.2）：写 `/etc/sysctl.d/99-dds-rx-buffers.conf`
   （含 `ipfrag_max_dist=4096`）、安装相机 USB 网卡 RPS 与开机相机恢复的 systemd unit、
   检查 CPU 是否满核在线；jetson-nx profile 的恢复流程同时校正相机为 CycloneDDS；
4. `docker compose build`（首次在设备上编译 COLMAP，约 20-40 分钟，只支持 Orin NX，不支持 Nano）；
5. 拉起 `./scripts/run_dashboard.sh`（可以 `--no-start` 跳过，只做环境准备）。

批量部署多台设备时不要每台都走这条路径重新编译——在一台机器上按 §1 打包，
其余设备走下面的使用者路径导入镜像即可，省去每台 20-40 分钟的 COLMAP 编译。
**注意**：目前只有 `jetson-nx` 这个 profile 会走 §1 打成正式发布镜像——`deploy/lite`、
`deploy/lite-779` 是两台开发机，只用 `select_device.sh` 切本地 config，不发布镜像。

### 3.2 使用者路径（拿到部署包 + 两个镜像 tar，机器上没有源码）

前提：JetPack 6.x、已装 docker 与 nvidia-container-runtime、当前用户在 `docker` 组里。
这几项本身不在 `update.sh` 的自动化范围内——它假设 docker 已经能跑。走这条路径前
逐条核对：

```bash
# 1. JetPack 版本——R36.x 对应 JetPack 6.x（R36.4 = 6.2.x），R35.x 是 JetPack 5，不满足要求
cat /etc/nv_tegra_release

# 2. docker 是否已装
command -v docker || sudo apt update && sudo apt install -y docker.io

# 3. docker compose v2 插件是否已装（"docker compose"，不是老的独立 "docker-compose" 命令）
# docker.io 不带这个插件；Ubuntu jammy 仓库里的包名是 docker-compose-v2（不是
# Docker 官方文档常提到的 docker-compose-plugin，那个包名在这个仓库源里没有）
docker compose version || sudo apt install -y docker-compose-v2

# 4. NVIDIA container runtime 是否已注册进 docker（"Runtimes:" 一行里要有 nvidia）
docker info 2>/dev/null | grep "Runtimes:"
# 没有就装：
sudo apt install -y nvidia-container-toolkit && sudo systemctl restart docker
# JetPack 出厂镜像通常已经带了这个包，但即使装了也不代表已经注册进 docker——
# 见过 apt 装完 nvidia-container-toolkit 之后 docker info 仍然没有 nvidia 这一项，
# 需要额外一步把它接进 docker 的 runtime 配置：
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
# 验证 GPU 能不能真正透传（不只是 runtime 注册上了）：
docker run --rm --network host --runtime=nvidia --gpus all ubuntu nvidia-smi

# 5. 当前用户在不在 docker 组（不在的话 docker 命令要 sudo 才能跑，run_dashboard.sh 等脚本没加 sudo 会直接报权限错）
groups | tr ' ' '\n' | grep -qx docker && echo "already in docker group" || {
    sudo usermod -aG docker "$USER"
    echo "已加入 docker 组，重新登录一次 shell（或重启）让组权限生效"
}

# 6. CPU 是否满核在线——nvpmodel 功耗模式可能把机器留在低功耗档（比如 15W 只开 4/6 核），
# docker-compose.yml 的 cpus 限额假设满核在线，不满足的话容器会直接创建失败
# （"range of CPUs is from 0.01 to 4.00"），不是跑得慢而是根本起不来
nproc   # 少于 6 就说明功耗模式没开满，且这台机器插电常驻、无功耗顾虑的话：
sudo nvpmodel -q                # 查看当前档位
sudo nvpmodel -m 0              # 切到 MAXN_SUPER 满血模式（会提示确认重启）
```

全新出厂设备如果这几项都没有，也可以直接走 §3.1 用 `setup_host.sh`——它跑的
就是同一套检查（见其 `# ── 1. docker + NVIDIA runtime ──` 一节），缺什么会明确
报错指出该装什么，不用照抄上面的命令逐条手动核对。

```bash
# 解压出的目录固定叫 insight-dashboard-deploy（不带版本号）——它就是这台设备的
# 常驻安装目录（.env、config/、rosbags/ 都在里面），名字跨版本不变，版本号只体现
# 在 tar 文件名和镜像 tag 上
tar xzf insight-dashboard-deploy-vX.Y.Z.tar.gz
cd insight-dashboard-deploy
./update.sh ../insight-dashboard-vX.Y.Z.tar.gz
```

首次安装时把 `insight-superglue-validation-25.04.tar.gz` 和 Dashboard 镜像放在
同一目录；`update.sh` 检测到本机缺少该依赖后会自动加载。日常升级不再需要它。

首次运行时 `update.sh` 会额外从镜像里把 `config/` 目录播种到宿主机（这台设备
还没有自己的机群配置/标定），此后这个 `config/` 就是本机独有的、不受镜像升级影响。

**装完 update.sh 之后，还有一步不能漏**：跑一次部署包自带的宿主机调优脚本
（`scripts/host_setup.sh` 由 `build_release.sh` 一起打进部署包，跟开发者路径
`setup_host.sh` 用的是同一份逻辑）——它会写内核 UDP 接收缓冲与 IP 分片调优、为相机
USB 网卡启用 RPS（把集中在 CPU0 的协议处理分散到其余核心），通过 udev 在 USB
重连、netdev 重建后自动恢复 RPS，并装好开机自动
恢复相机的 systemd unit。jetson-nx profile 还会把相机 DDS 模式幂等校正为
CycloneDDS；FastDDS 的约 65 KB UDP 报文会在多路大图订阅时触发分片/重传风暴。
缺少这些调优时，多路录制会在写盘之前丢包，
开机恢复 unit 则处理相机与 Jetson 的 DDS 启动时序
（相机比 Jetson 先启动完，DDS participant 会卡在错误的网络状态，见其脚本注释）。
这些宿主机配置不在 `update.sh` 的职责范围内（它只管容器生命周期），必须在宿主机
单独跑一次（脚本按需调用 `sudo`）：

```bash
./scripts/host_setup.sh
```

首次安装完成后，如果这台设备接的是跟别的 jetson-nx 设备不同的相机机群，
`config/cameras.json` 里的相机列表是 profile 的默认值；如果这台设备接的是
不同的相机机群，需要按实际命名空间和全局建图/重定位 topic 调整。固定机群
（本项目目前是：insight3_a / insight3_b / insight9_a）无需额外执行轨迹对齐操作。

浏览器访问 `http://<设备IP>:8765/`，或 `./scripts/run_dashboard.sh --jetson`
在设备屏幕本机打开全屏看板。日常操作和故障排查见 `docs/USAGE.md`。
