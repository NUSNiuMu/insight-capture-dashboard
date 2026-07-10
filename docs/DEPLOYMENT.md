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

---

## 1. 开发者手册：怎么编译发布镜像

### 1.1 前提

- 在 Jetson（arm64）上编译，不能在 x86 开发机上交叉编译当前的 Dockerfile
  （COLMAP 的 CUDA sm_87 编译在 `colmap-builder` 阶段，依赖宿主机的 NVIDIA 工具链）。
- 当前分支就是要发布的版本——`build_release.sh` 直接 `docker build` 工作目录，
  发布前先确认 `git status` 干净、该合的改动都合并了。
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
部署包里打包了 `deploy/docker-compose.yml`、`update.sh`、`README.md`、
`scripts/run_dashboard.sh`——这几个文件本身不大，但**它们的内容来自当前
分支的 `deploy/` 目录**，改过 `deploy/docker-compose.yml`（比如调 shm_size）
一定要在改动落地之后的这个分支上重新跑一次 `build_release.sh`，不能沿用
旧的部署包。

### 1.3 版本号约定

这个仓库目前还没有为发布流程打过 git tag（`git tag` 里唯一的旧标签跟这套
发布脚本无关）。建议：镜像版本号对应到一个 git tag（`git tag v1.2.0 && git push --tags`），
方便日后从版本号反查代码状态；`build_release.sh` 本身不会自动打 tag，需要手动做。

### 1.4 发布前检查清单

- [ ] 本地开发机 compose 跑过一遍，关键页面（3d / images / recording / scoring / settings）无 console 报错
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
git clone -b deploy/jetson-nx git@github.com:NUSNiuMu/insight-capture-dashboard.git insight_capture
cd insight_capture
./scripts/setup_host.sh
```

`setup_host.sh` 是幂等的一次性宿主机配置，按顺序做：

1. 检查 docker + NVIDIA container runtime 是否就绪（硬件 JPEG/H.264 编解码依赖它注入 GStreamer 插件）；
2. 写 `/etc/sysctl.d/99-dds-rx-buffers.conf`（需要 sudo）——不做这一步，录制会因内核 UDP
   接收缓冲区太小丢 10-24% 的图像帧；
3. 安装并启用开机自动重启相机的 systemd unit（相机比 Jetson 先启动完成，会卡在错误的网络状态）；
4. `docker compose build`（首次在设备上编译 COLMAP，约 20-40 分钟，只支持 Orin NX，不支持 Nano）；
5. 拉起 `./scripts/run_dashboard.sh`（可以 `--no-start` 跳过，只做环境准备）。

批量部署多台设备时不要每台都走这条路径重新编译——在一台机器上按 §1 打包，
其余设备走下面的使用者路径导入镜像即可，省去每台 20-40 分钟的 COLMAP 编译。

### 3.2 使用者路径（拿到部署包 + 镜像 tar，机器上没有源码）

前提：JetPack 6.x、已装 docker 与 nvidia-container-runtime、当前用户在 `docker` 组里。
这几项本身不在 `update.sh` 的自动化范围内——它假设 docker 已经能跑；全新出厂设备如果
连 docker 都没装，先按 JetPack/NVIDIA 官方文档把这一层跑通，或者走 §3.1 用
`setup_host.sh`（它会检查这些前提并在缺失时明确报错指出该装什么）。

```bash
tar xzf insight-dashboard-deploy-vX.Y.Z.tar.gz
cd insight-dashboard-deploy-vX.Y.Z
./update.sh insight-dashboard-vX.Y.Z.tar.gz
```

首次运行时 `update.sh` 会额外从镜像里把 `config/` 目录播种到宿主机（这台设备
还没有自己的机群配置/标定），此后这个 `config/` 就是本机独有的、不受镜像升级影响。

首次安装完成后，`config/cameras.json` 里的相机列表、`config/board_calibration.json`
的标定板参数都是占位/上一台设备遗留的默认值——**必须针对这台设备实际连接的
相机、实际使用的标定板重新走一遍配置和标定**，不能假设装完就能直接用，
即使镜像是从另一台已标定好的设备打包出来的。

浏览器访问 `http://<设备IP>:8765/`，或 `./scripts/run_dashboard.sh --jetson`
在设备屏幕本机打开全屏看板。日常操作和故障排查见 `docs/USAGE.md`。
