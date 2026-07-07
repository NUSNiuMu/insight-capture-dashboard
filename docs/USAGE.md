# Insight Capture Dashboard — 使用与运维手册

> 适用分支：`deploy/jetson-nx`（Orin NX，含 NVJPEG 硬件编码）。
> `deploy/lite`（Orin Nano，无硬件编码）除"硬件加速"一节外全部通用。
> 功能细节（标定参数、avatar 配置等）见 [README.md](../README.md)，本文侧重**部署、日常操作、故障排查**。

---

## 1. 系统概述

多路 Insight 相机的实时监控与数据采集系统：

```
Insight 相机 ×3 ──USB 网口(169.254.x.x 点对点)──> Jetson Orin NX
                                                    │
                              docker 容器 (host network, restart: unless-stopped)
                              ├─ ROS2 订阅：图像 / IMU / VIO pose
                              ├─ NVJPEG 硬件 JPEG 编码（显示路径）
                              ├─ 进程内 rosbag 录制（不丢录制帧的关键设计）
                              └─ aiohttp Web 服务 :8765
                                                    │
                     浏览器（本机 kiosk / SSH 隧道远程） http://localhost:8765
```

| 组件 | 说明 |
|---|---|
| 后端 | `scripts/multi_camera_dashboard_web.py`，容器内常驻 |
| 前端 | `web_dashboard/dist/`，Babylon.js，改源码后需 `node build.js` 或手动同步 |
| 配置 | `config/cameras.json`（唯一常改入口），`config/board_calibration.json` |
| 数据 | `rosbags/`（录制输出），`outputs/`（评分等结果） |

---

## 2. 环境要求

| 项 | 要求 | 自检命令 |
|---|---|---|
| 硬件 | Jetson Orin NX（Nano 用 `deploy/lite`） | `cat /etc/nv_tegra_release` |
| 系统 | JetPack 6.x（L4T R36+） | 同上 |
| Docker | 含 NVIDIA container runtime | `docker info \| grep -i runtimes` |
| 内核参数 | UDP 接收缓冲 ≥64MB（见 §3） | `sysctl net.core.rmem_max` |
| 相机 | 每台独占一个 USB 网口，`169.254.x.x/24` | `ip -4 -br addr \| grep 169.254` |
| ROS 域 | `ROS_DOMAIN_ID=20`（与相机一致） | `config/cameras.json` |

---

## 3. 一键启动

### 3.1 全新机器首次部署（一条命令）

```bash
git clone -b deploy/jetson-nx git@github.com:NUSNiuMu/insight-capture-dashboard.git insight_capture
cd insight_capture
./scripts/setup_host.sh        # 需要 sudo 密码
```

`setup_host.sh` 幂等（可反复执行），依次完成：

1. 检查 docker + NVIDIA runtime（硬件编码依赖 runtime 注入的 GStreamer 插件）；
2. 写入 `/etc/sysctl.d/99-dds-rx-buffers.conf`——**必做**，否则录制会静默丢
   10-24% 图像帧（内核默认 208KB UDP 缓冲装不下 510KB 的单帧图像样本）；
3. `docker compose build`（首次约 2GB 下载，之后有缓存）；
4. 转入 `run_dashboard.sh` 启动。

> **刷机后必须重跑一次**：sysctl 文件在宿主机上，重刷 JetPack 会丢。

### 3.2 日常启动

```bash
./scripts/run_dashboard.sh             # 启动/确保运行，打印远程访问方式
./scripts/run_dashboard.sh --jetson    # 额外拉起本机全屏 kiosk（接显示器时）
./scripts/run_dashboard.sh --logs      # 同时跟随后端日志（含 CPU 分解）
```

脚本自带健康检查与自愈：等待 `/healthz`、等待至少一路相机出数据；若"相机
网口在但 10 秒无数据"会自动重启一次后端（DDS participant 早于相机网口创建
的经典竞态）。容器 `restart: unless-stopped`——断电重启、SSH 断开都会自动拉起。

停止：前台 Ctrl-C，或 `docker compose down`。

### 3.3 远程访问（推荐，不暴露端口）

```bash
# 在自己电脑上：
ssh -L 8765:localhost:8765 nvidia@<jetson-ip>
# 浏览器打开 http://localhost:8765/
```

---

## 4. 页面与日常操作

| 路径 | 用途 |
|---|---|
| `/` 或 `/3d` | 3D VIO 轨迹 + 在线标定按钮 |
| `/images` | 三路相机实时画面（当前为 JPEG 轮询显示，约 10fps 上限，见 §7.7） |
| `/recording` | rosbag 录制：topic 发现、勾选、开始/停止、同步到主机 |
| `/bags` | 本地 bag 列表：大小/时长/评分状态 |
| `/scoring` | 轨迹评分 |
| `/settings` | 手部叠加开关、夹爪追踪、标定参数、**图像管线能力诊断**、重启后端 |

### 4.1 标准采集流程

1. `./scripts/run_dashboard.sh`，确认 `/images` 三路画面都是活的（面板无 stale 标记）；
2. `/recording` → `Refresh Topics` → 勾选（支持按相机整组勾选）→ `Start`；
3. 采集完成 → `Stop`；
4. **立刻校验数据完整性**（30 秒内出结论）：

```bash
python3 scripts/check_bag.py            # 检查最新一份 bag，退出码 0=完整
python3 scripts/check_bag.py rosbags/insight_record_20260707_173821   # 指定 bag
```

输出对每个 topic 给出 `实收条数 / 实测频率 / 估计缺失% / 最大断口位置`，
任一 topic 缺失超过 0.5%（默认阈值，`--max-loss` 可调）即 FAIL 并给出排查入口。
录制开始后前 2 秒的订阅预热断口默认忽略（`--warmup`）。

### 4.2 API 速查

```
GET  /healthz                        存活探针
GET  /api/cameras                    相机列表 + fps/分辨率/stale
GET  /api/cameras/<name>/frame       该相机当前帧 JPEG
GET  /api/images/capabilities        编码管线诊断（硬件路径是否生效）
GET  /api/recording/status           录制状态
POST /api/recording/start|stop       录制控制（start 可带 {"topics": [...]}）
GET  /api/rosbags                    bag 列表
GET  /api/alignment                  标定状态
```

---

## 5. 硬件加速（deploy/jetson-nx 专有）

显示路径的 JPEG 编码跑在 Orin NX 的 NVJPEG 专用引擎上（`scripts/hw_jpeg.py`），
不占 CPU、不占 GPU（CUDA），实测比软件编码省 4-5.6 倍 CPU 时间。

- 生效确认：`/settings` 页 Image capabilities 显示
  **"Display path: hardware JPEG encode (NVJPEG engine)"**，
  或 `curl -s localhost:8765/api/images/capabilities | python3 -m json.tool`
  中 `active_path = "jpeg-hardware-nvjpeg"`、`hw_jpeg.active = true`；
- 失效不致命：任何环节失败自动回落 cv2 软件编码，画面照常，只是 CPU 变高
  （排查见 §7.6）；
- 手部叠加开启时的解码/重编码同样走硬件。

---

## 6. 版本与分支

| 分支 | 目标设备 | 差异 |
|---|---|---|
| `deploy/jetson-nx` | Orin NX | 含 NVJPEG 硬件编码 + 容器内 GStreamer 依赖 |
| `deploy/lite` | Orin Nano | 无硬件编码，无 COLMAP 依赖 |
| `main` | 开发机 Jetson | 含 COLMAP/optimization 的宿主机挂载 |

升级：`git pull` 后重跑 `./scripts/run_dashboard.sh` 即可（代码 live-mount，
改了 `Dockerfile` 才需要 `docker compose build`）。客户离线部署用
`scripts/build_release.sh` + `deploy/` 目录的镜像包流程。

---

## 7. 常见问题排查

> 通用三板斧，任何异常先跑：
> ```bash
> docker ps                                    # 容器活着吗
> curl -s localhost:8765/healthz               # HTTP 活着吗
> docker logs insight-dashboard --since 10m | grep -iE "error|warn" | tail
> ```

### 7.1 页面打不开

| 检查 | 命令 | 处理 |
|---|---|---|
| 容器没起 | `docker ps -a \| grep insight` | `./scripts/run_dashboard.sh`；看 `docker logs` 找崩溃原因 |
| 端口不对 | `docker exec insight-dashboard printenv DASHBOARD_PORT` | 默认 8765；compose 里改过要同步隧道命令 |
| 远程访问没建隧道 | — | 见 §3.3；后端只监听本机，不建隧道连不上属预期 |

### 7.2 相机画面黑/stale（最常见）

**症状**：`/images` 面板灰掉或标 stale，`/api/cameras` 里 `stale: true`。

按顺序排查：

1. **物理链路**：`ip -4 -br addr | grep 169.254` —— 每台在线相机应有一个
   `169.254.x.2/24` 接口。没有 → 查 USB 线/相机供电，或
   `./scripts/reboot_cameras.sh`；
2. **DDS 竞态（开机后一直没数据的典型原因）**：容器随开机自启，常早于相机
   网口出现，DDS participant 绑不到相机链路且**不会自愈**。
   处理：`docker restart insight-dashboard`。
   注：`run_dashboard.sh` 启动时和后端内置 watchdog（链路在但 60 秒零消息
   时自动退出重启）都已自动化这一步，手动重启是兜底；
3. **域号不匹配**：相机和 dashboard 必须同 `ROS_DOMAIN_ID`（当前 20）。
   验证：`docker exec insight-dashboard bash -ic "ros2 topic list" | head`；
4. **单路没数据、其余正常**：基本是那台相机自身问题（供电/自身 ROS 栈），
   重启该相机。

### 7.3 录制掉帧 / 数据不完整

**症状**：`check_bag.py` FAIL；图像 topic 实测帧率低于标称；下游算法报数据断口。

```bash
python3 scripts/check_bag.py rosbags/<bag>     # 先量化：哪个 topic、丢多少、断口在哪
```

诊断树：

1. **只有图像丢、camera_info/IMU 完整** → DDS/UDP 接收侧丢包（大样本 vs 小缓冲）。
   检查内核参数是否被重置（刷机后会丢）：
   ```bash
   sysctl net.core.rmem_max        # 必须是 67108864，若是 212992 → 重跑 ./scripts/setup_host.sh
   ```
   2026-07-07 实测：默认 208KB 缓冲下三路图像丢 10-24%，修复后 0%；
2. **所有 topic 在同一时间窗一起断** → 接收端系统级事件，看当时 CPU：
   `docker stats insight-dashboard`，排查是否有评分/优化任务、kiosk 浏览器抢核；
3. **某台相机自己的 topic 集体断（含它的 IMU/VIO）** → 相机侧停顿，
   与本机无关（曾观察到 insight3_b 的 VIO 单次中断 2.5s），复现则联系相机侧排查；
4. **日志出现 writer 队列丢帧** → 磁盘写入跟不上，查 §7.5 磁盘。

> 设计背景：录制在订阅回调内直接入队（进程内 writer），回调近零成本，
> 所以"到达即录上"；丢帧几乎总是发生在**到达之前**（网络/DDS 层）。

### 7.4 录不上 / 录制启动失败

- `/api/recording/status` 看 `recording` 与 `output_path`；
- topic 勾选后 Start 无反应 → `Refresh Topics` 重新发现（相机刚重启过时
  topic 列表会过期）；
- 注意：`run_dashboard.sh` 检测到录制进行中不会重启后端，但手动
  `docker restart` **会杀掉录制**，操作前先看状态。

### 7.5 磁盘满

```bash
df -h /                                  # 916MB/45s ≈ 1.2GB/min，三路全录很快
du -sh rosbags/* | sort -h | tail       # 找大文件
```

删除：`/bags` 页面删，或 `curl -X DELETE localhost:8765/api/rosbags/<bag_name>`，
或直接 `rm -rf rosbags/<bag_name>`（确认已同步/不需要后）。

### 7.6 硬件编码没生效（CPU 异常高）

**症状**：`/settings` 显示 "Display path: software JPEG (cv2)"，或
`docker stats` 里 CPU 明显高于正常值（正常三路 ~100% 单核上下）。

```bash
curl -s localhost:8765/api/images/capabilities | python3 -m json.tool
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `gstreamer.available: false` | 镜像缺 GStreamer（分支/镜像不对） | 确认在 `deploy/jetson-nx` 分支并 `docker compose build` |
| `elements.nvjpegenc: false` | NVIDIA runtime 没注入插件 | `docker inspect insight-dashboard \| grep Runtime` 应为 nvidia；查 `/etc/nvidia-container-runtime/host-files-for-container.d/drivers.csv` 里有 `libgstnvjpeg.so` |
| `hw_jpeg.disabled` 有条目 | 管线连续失败已自动禁用 | `docker logs insight-dashboard \| grep hw_jpeg` 看具体报错，`docker restart insight-dashboard` 重试 |
| 在 Nano 上 | Nano 无此硬件 | 属预期，用 `deploy/lite` |

### 7.7 画面卡顿 / 帧率低

- 前端显示上限约 **10fps**（100ms 轮询设计），后端实际 20fps——这是当前
  传输方案的已知上限，不是故障；高帧率显示方案（WebRTC + 硬件 H.264）在
  规划中；
- 低于 10fps 时：`/api/cameras` 看后端 `fps` 是否满 20/30 → 满则查浏览器
  端（换 Chrome/关其他标签页）；不满则按 §7.2/§7.3 查链路。

### 7.8 手部叠加骨架闪烁

已知现象，三个来源：检测偶发丢手时该帧不叠加（骨架闪断）；骨架取最新
检测结果、未与图像帧做时间戳配对（快速移动时跳动）；显示链路抽帧。
硬件编码已降低单帧成本缓解第三条；前两条的修复（骨架保持窗口 + 时间戳
配对）在待办中。不影响录制数据——叠加只发生在显示路径，bag 里是原始图。

### 7.9 在线标定不收敛

见 [README.md](../README.md) "标定排查要点"。速查：
`curl localhost:8765/api/cameras/<name>/frame -o /tmp/f.jpg` 确认板子在视野内
且 marker 成像 ≥60px；诊断日志 `/tmp/insight_live_alignment.log`（容器内）。

### 7.10 时间/时区问题

bag 名与日志用 `TZ=Asia/Shanghai`（compose 里设定）。若 bag 名时间差 8 小时,
检查 compose 环境变量是否被改动。相机与主机时间不同步会影响多机数据对齐，
录制前确认 `date` 与相机侧一致。

### 7.11 kiosk 白屏/不显示（--jetson）

- `echo $DISPLAY`（本地桌面会话通常 `:0`），SSH 登录的 shell 需
  `DISPLAY=:0 ./scripts/run_dashboard.sh --jetson`；
- compose 的 `shm_size: 2gb` 是 Chromium GPU 进程的硬需求，别删；
- kiosk 只是个浏览器，白屏先用远程浏览器访问同一地址排除后端问题。

---

## 8. 排查信息收集（报障时请附带）

```bash
{
  date; git -C ~/insight_capture branch --show-current; git -C ~/insight_capture log -1 --oneline
  cat /etc/nv_tegra_release | head -1
  sysctl net.core.rmem_max
  docker ps -a | grep insight
  curl -s localhost:8765/api/images/capabilities
  curl -s localhost:8765/api/cameras
  ip -4 -br addr | grep 169.254
  docker logs insight-dashboard --since 30m 2>&1 | tail -100
} > /tmp/insight_diag_$(date +%Y%m%d_%H%M%S).txt 2>&1
```
