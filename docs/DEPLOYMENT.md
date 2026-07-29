# 部署与发布手册

本仓库的镜像化部署/升级流程涉及三类人、三个入口脚本：

| 谁 | 做什么 | 用哪个脚本 |
|---|---|---|
| 开发者 | 把当前分支的代码编译成一个可分发的镜像包 | `scripts/build_release.sh` |
| 使用者 | 拿到镜像包，给已经在跑的设备升级/回滚 | `deploy/update.sh` |
| 任何人 | 全新 Jetson 首次部署 | `scripts/setup_host.sh`（开发者路径）或 `deploy/update.sh`（使用者路径，见下） |

三者的关系：`build_release.sh` 在开发机上跑，产出「镜像 tar」+「部署包 tar」两个文件；
部署包只在**首次安装**时需要（它带着 `update.sh` 本身、`docker-compose.yml`、
`README.md`）；之后每次升级只需要发一个新的镜像 tar，使用者已有的 `update.sh` 不变。

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
  把要发布的改动完整跑一遍、`/verify` 或手动过一遍关键页面，**不要用没跑过的代码直接打包发布**。

### 1.2 打包

```bash
./scripts/build_release.sh v1.2.0
```

版本号必须形如 `vX.Y.Z`（可带 `-rc1` 之类后缀），脚本会校验格式。产出：

```
release/insight-dashboard-v1.2.0.tar.gz          # 镜像；每次升级都发这个
release/insight-dashboard-deploy-v1.2.0.tar.gz   # 部署包；只有首次安装的设备需要
```

镜像 tar 通常几 GB（含 COLMAP、CUDA 运行时、Chromium），传输/拷 U 盘预留时间。
自 v2.0.0 起，`build_release.sh` 会额外构建并保存
`insight-superglue-validation:25.04`（Insight9 建图/Insight3 全局重定位依赖的
Magic Leap 官方 SuperPoint/SuperGlue TensorRT 推理，商用分发已确认，见
`docs/INSIGHT9_SPARSE_MAPPING.md`）到同一个镜像 tar，体积和传输时间进一步增加。
部署包里打包了 `deploy/docker-compose.yml`、`update.sh`、`README.md`、
`scripts/run_dashboard.sh`，以及宿主机一次性调优用的 `scripts/host_setup.sh`
+ `scripts/reboot_cameras.sh` + `scripts/systemd/insight-camera-reboot.service`
+ `looper_cli/`（2026-07-12 补的——在此之前使用者路径完全没有办法应用这几项
host 层设置，见 §3.2）——这几个文件本身不大，但**它们的内容来自当前
分支的 `deploy/`、`scripts/`、`looper_cli/` 目录**，改过 `deploy/docker-compose.yml`
（比如调 shm_size）或 `scripts/host_setup.sh` 一定要在改动落地之后的这个分支上
重新跑一次 `build_release.sh`，不能沿用旧的部署包。

### 1.3 版本号约定

这个仓库目前还没有为发布流程打过 git tag（`git tag` 里唯一的旧标签跟这套
发布脚本无关）。建议：镜像版本号对应到一个 git tag（`git tag v1.2.0 && git push --tags`），
方便日后从版本号反查代码状态；`build_release.sh` 本身不会自动打 tag，需要手动做。

### 1.4 发布前检查清单

- [ ] 本地开发机 compose 跑过一遍，关键页面（3d / recording / bags / scoring / handpose / settings）无 console 报错
- [ ] `superglue-inference` 健康检查能通过（本地至少验证一次冷启动 TensorRT 引擎编译），
  `insight9-sparse-mapper`/`insight3-global-localizer` 正常起来，3d 页面能看到全局轨迹
- [ ] 涉及数据库结构/配置字段变化的改动，确认旧版本升级上来后不会因为缺字段崩溃
  （`config/` 目录在首次安装后不会被镜像覆盖，见下文"数据持久化"）
- [ ] `git status` 干净，且要发布的 commit 已经推到远端（发布包本身不含 git 历史，事后排查靠 commit hash）
- [ ] 如果这次发布改了 `Dockerfile`（新依赖），本地至少验证过 `docker compose build` 全新构建成功一次（不是吃的旧层缓存）

### 1.5 交付

- **首次安装**：把镜像 tar + 部署包 tar 都发给对方（U 盘/scp 均可）。
- **升级**：只发镜像 tar，对方在已有的部署目录里跑 `./update.sh <镜像tar>`。

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
- **旧版本不会被自动清理**：`docker load` 过的镜像会一直留着，方便回滚；磁盘紧张时自己
  `docker image ls insight-dashboard` 查、`docker rmi insight-dashboard:<旧版本>` 清理不需要的。

### 2.3 回滚

```bash
./update.sh --rollback v1.1.0
```

要求 `v1.1.0` 这个镜像之前 `docker load` 过（还在本机）。等价于把 `.env` 指回旧版本号
再 `docker compose up -d`，同样过一遍 healthz 检查。

### 2.4 升级失败排查

| 现象 | 大概率原因 | 怎么查 |
|---|---|---|
| `docker load` 报错 / 卡住 | 镜像 tar 传输不完整 | 检查文件大小、`md5sum`/`sha256sum` 跟发布方核对 |
| healthz 90 秒超时 | 后端容器起不来 | `docker compose logs -f`；常见于新版本引入的依赖没装全（对照 §1.4 发布前检查） |
| 升级后配置/标定"消失" | 罕见——config/ 首次安装后不该被覆盖 | 确认没有手动删过 `config/` 目录；`ls config/` 核对文件还在 |
| "a recording is in progress" | 有正在跑的录制 | 等它录完，或确认可以中断后加 `--force` |

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
3. 调用 `scripts/host_setup.sh`（与使用者路径共用，见 §3.2）：写 `/etc/sysctl.d/99-dds-rx-buffers.conf`、
   安装并启用开机自动重启相机的 systemd unit、检查 CPU 是否满核在线；
4. `docker compose build`（首次在设备上编译 COLMAP，约 20-40 分钟，只支持 Orin NX，不支持 Nano）；
5. 拉起 `./scripts/run_dashboard.sh`（可以 `--no-start` 跳过，只做环境准备）。

批量部署多台设备时不要每台都走这条路径重新编译——在一台机器上按 §1 打包，
其余设备走下面的使用者路径导入镜像即可，省去每台 20-40 分钟的 COLMAP 编译。
**注意**：目前只有 `jetson-nx` 这个 profile 会走 §1 打成正式发布镜像——`deploy/lite`、
`deploy/lite-779` 是两台开发机，只用 `select_device.sh` 切本地 config，不发布镜像。

### 3.2 使用者路径（拿到部署包 + 镜像 tar，机器上没有源码）

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

首次运行时 `update.sh` 会额外从镜像里把 `config/` 目录播种到宿主机（这台设备
还没有自己的机群配置/标定），此后这个 `config/` 就是本机独有的、不受镜像升级影响。

**装完 update.sh 之后，还有一步不能漏**：跑一次部署包自带的宿主机调优脚本
（`scripts/host_setup.sh` 由 `build_release.sh` 一起打进部署包，跟开发者路径
`setup_host.sh` 用的是同一份逻辑）——它做两件事：写内核 UDP 接收缓冲区调优
（不做这步录制会丢 10-24% 图像帧）、装并启用开机自动重启相机的 systemd unit
（相机比 Jetson 先启动完，DDS participant 会卡在错误的网络状态，见其脚本注释）。
这两项不在 `update.sh` 的职责范围内（它只管容器生命周期），必须单独跑一次：

```bash
./scripts/host_setup.sh
```

首次安装完成后，如果这台设备接的是跟别的 jetson-nx 设备不同的相机机群，
`config/cameras.json` 里的相机列表是 profile 的默认值；如果这台设备接的是
不同的相机机群，需要按实际命名空间和全局建图/重定位 topic 调整。固定机群
（本项目目前是：insight3_a / insight3_b / insight9_a）无需额外执行轨迹对齐操作。

浏览器访问 `http://<设备IP>:8765/`，或 `./scripts/run_dashboard.sh --jetson`
在设备屏幕本机打开全屏看板。日常操作和故障排查见 `docs/USAGE.md`。
