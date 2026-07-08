# Insight Capture Dashboard — 使用手册

> 设备出厂前已完成全部环境配置（Docker、NVIDIA 运行时、内核参数、软件安装），
> 本手册不含安装步骤，只覆盖**日常使用、数据采集与故障排查**。
> 软件升级方式见 §5；开发者功能参考见仓库 [README.md](../README.md)。

---

## 1. 系统概述

多路 Insight 相机的实时监控与数据采集系统：

```
Insight 相机 ×3 ──USB 网口──> Jetson 主机 ──docker 容器──> 浏览器
                                │                          http://<设备IP>:8765
                                ├─ 实时画面 / 3D VIO 轨迹
                                ├─ rosbag 数据录制
                                └─ 在线标定 / 评分
```

- 服务随开机自启（断电重启后自动恢复），正常情况下**无需任何手动启动操作**；
- 设备与相机一起上电时，系统会在开机后**自动重启一次所有相机**（相机比主机
  先启动完成会处于错误的网络状态，属预期行为）——从上电到画面就绪约 2-3 分钟；
- 每台相机独占一个 USB 网口（`169.254.x.x` 段），插拔顺序不影响使用；
- 所有数据（录制、标定、配置）保存在设备的 `rosbags/`、`config/`、`outputs/`
  目录，软件升级不会触碰它们。

---

## 2. 启动与访问

### 2.1 一键启动

服务开机自启，通常直接访问即可。需要手动确认/重启时：

```bash
./scripts/run_dashboard.sh             # 启动或确认后端在跑；Ctrl-C 停止
./scripts/run_dashboard.sh --jetson    # 同时在设备屏幕上打开全屏看板（接显示器时）
./scripts/run_dashboard.sh --logs      # 同时跟随后端日志
```

脚本自带健康检查与自愈：等待服务就绪、等待相机出数据；若检测到"相机网口
已连接但迟迟无数据"（开机时序问题）会自动重启一次后端。可随时重复执行。

停止服务：前台 Ctrl-C，或 `docker compose down`（在部署目录下执行）。

### 2.2 浏览器访问

- 同一局域网：`http://<设备IP>:8765/`
- 或 SSH 隧道（不依赖局域网可达）：
  ```bash
  ssh -L 8765:localhost:8765 <用户名>@<设备IP>
  # 然后浏览器打开 http://localhost:8765/
  ```

---

## 3. 页面与日常操作

| 路径 | 用途 |
|---|---|
| `/` 或 `/3d` | 3D VIO 轨迹 + 在线标定按钮 |
| `/images` | 三路相机实时画面 |
| `/recording` | rosbag 录制：topic 发现、勾选、开始/停止 |
| `/bags` | 本地录制列表：大小/时长/完整性/评分/优化状态，可删除 |
| `/scoring` | 轨迹评分 + 录制完整性验证 |
| `/settings` | 手部叠加、夹爪追踪、标定参数、图像管线诊断、重启后端 |

### 3.1 标准采集流程

1. 打开 `/images`，确认三路画面都在动（面板无 stale 灰标）；
2. `/recording` → `Refresh Topics` → 勾选要录的 topic（支持按相机整组勾选）→ `Start`；
3. 采集完成 → `Stop`；
4. **立刻校验数据完整性**（约 10 秒出结论）：
   打开 `/scoring` 页，选中刚录的 bag，点 **Verify Integrity**。
   结果面板逐 topic 给出 `实收条数 / 实测频率 / 丢失% / 最大断口位置`，
   顶部绿色 **Complete** 即数据完整；红色 **Incomplete** 时按 §6.3 排查。
   验证结果会持久保存，`/bags` 列表中该 bag 会带上绿色 `complete` /
   红色 `incomplete` 徽章（未验证过的显示灰色 `unverified`）。

命令行等价方式（脚本化/无浏览器时）：

```bash
docker exec insight-dashboard python3 scripts/check_bag.py                # 最新一份就可以
docker exec insight-dashboard python3 scripts/check_bag.py rosbags/<目录名>
```

### 3.2 磁盘管理

三路全录约 **1.2GB/分钟**，录制前确认空间：

```bash
df -h /                              # 剩余空间
du -sh rosbags/* | sort -h | tail    # 各录制占用
```

删除：`/bags` 页面操作，或直接删除 `rosbags/` 下对应目录（确认已拷贝/不需要后）。

---

## 4. 在线标定（多相机轨迹对齐）

操作入口在 `/3d` 页右上角 `Start / Stop Alignment`。流程与参数详见仓库
[README.md](../README.md) 的"在线轨迹标定"一节，操作要点：

1. 让每台待标定相机稳定看到同一块 AprilTag 标定板几秒（无需多台同时看到）；
2. 页面状态从 `samples n/12` 攒满后自动进入 `tracking`；
3. 全部标完点 `Stop Alignment`，结果保留并持续用于轨迹显示。

标定不收敛时：确认板子确实在该相机视野内且成像足够大
（`http://<设备IP>:8765/api/cameras/<相机名>/frame` 可直接查看该相机当前画面）。

---

## 5. 软件升级

升级只需要一个新的镜像压缩包（`insight-dashboard-vX.Y.Z.tar.gz`），
在部署目录执行：

```bash
./update.sh insight-dashboard-vX.Y.Z.tar.gz
```

录制数据、标定结果、配置全部保留。回滚：把部署目录 `.env` 里的版本号改回
上一个已加载的版本，`docker compose up -d` 即可。

---

## 6. 常见问题排查

> 任何异常先跑通用三板斧：
> ```bash
> docker ps | grep insight                     # 1. 容器活着吗（STATUS 应为 Up）
> curl -s localhost:8765/healthz               # 2. 服务活着吗（应返回 ok）
> docker logs insight-dashboard --since 10m 2>&1 | grep -iE "error|warn" | tail
> ```

### 6.1 页面打不开

| 检查 | 命令/方法 | 处理 |
|---|---|---|
| 容器没起来 | `docker ps -a \| grep insight` | `./scripts/run_dashboard.sh`；仍失败看 `docker logs insight-dashboard` |
| 网络不可达 | 能否 `ping <设备IP>` | 检查设备联网，或改用 SSH 隧道（§2.2） |
| 浏览器缓存 | 强制刷新 Ctrl+Shift+R | — |

### 6.2 相机画面黑 / 灰标 stale（最常见）

按顺序排查，命中即止：

1. **物理链路**：`ip -4 -br addr | grep 169.254` —— 每台在线相机应对应一行。
   缺行 → 检查该相机 USB 线和供电，或执行 `./scripts/reboot_cameras.sh`
   （开发机；客户机可直接给相机断电重启）；
2. **开机时序问题**（开机后所有相机一直无数据的典型原因）：
   `docker restart insight-dashboard`，30 秒内应恢复。
   说明：服务比相机网口先启动时会绑不上相机链路且不自愈；启动脚本和后端
   看门狗已自动处理绝大多数情况，手动重启是兜底；
3. **单路无数据、其余正常**：该相机自身问题，断电重启该相机；
4. 以上无效：收集 §7 诊断信息报障。

### 6.3 录制掉帧 / 数据不完整

**症状**：`/scoring` 页 Verify Integrity 显示红色 Incomplete（或 `/bags`
列表出现红色 `incomplete` 徽章、命令行 `check_bag.py` 报 FAIL）。

按 FAIL 的模式判断：

1. **只有 image topic 丢、camera_info/IMU 完整** → 内核 UDP 接收缓冲被重置
   （常见于系统重刷后）。验证与恢复：
   ```bash
   sysctl net.core.rmem_max          # 正常应为 67108864；若为 212992 即命中
   ```
   恢复（一次性，重启后保持）：
   ```bash
   sudo tee /etc/sysctl.d/99-dds-rx-buffers.conf >/dev/null <<'EOF'
   net.core.rmem_max = 67108864
   net.core.rmem_default = 67108864
   net.ipv4.ipfrag_high_thresh = 134217728
   EOF
   sudo sysctl -p /etc/sysctl.d/99-dds-rx-buffers.conf
   docker restart insight-dashboard
   ```
   然后重录一段用 `check_bag.py` 复验（此问题实测丢帧 10-24%，修复后为 0）；
2. **所有 topic 在同一时间段一起断** → 录制期间设备被其他任务抢占，
   `docker stats insight-dashboard` 观察 CPU；录制时避免同时跑评分/优化任务；
3. **某台相机自己的全部 topic（含 IMU/VIO）同时断** → 相机侧停顿，
   与主机无关；复现请记录相机名与时间点后报障；
4. **磁盘写满**：见 §3.2。

### 6.4 录制无法开始 / 停止异常

- 先看状态：`curl -s localhost:8765/api/recording/status`；
- `Start` 无反应：点一次 `Refresh Topics` 再试（相机刚重启过时 topic 列表会过期）；
- 注意：录制进行中**不要**执行 `docker restart`，会中断录制。
  `run_dashboard.sh` 已内置保护（检测到录制中不重启），手动 docker 命令没有。

### 6.5 画面卡顿 / 帧率低

- 显示帧率上限约 10fps 属当前设计（录制数据不受影响，bag 里是相机原生帧率）；
- 明显低于 10fps：先看 `curl -s localhost:8765/api/cameras` 中各路 `fps`
  是否为 20/30 —— 是则换浏览器/关闭其他标签页；否则按 §6.2 查相机链路。

### 6.6 CPU 占用异常高（硬件编码回退）

正常三路相机约占一个核（`docker stats insight-dashboard` 的 CPU% ≈100%）。
明显偏高时检查硬件编码是否在用：

```bash
curl -s localhost:8765/api/images/capabilities | python3 -m json.tool
```

`active_path` 应为 `jpeg-hardware-nvjpeg`（`/settings` 页也有同样的诊断卡片）。

| 现象 | 处理 |
|---|---|
| `hw_jpeg.disabled` 里有条目 | `docker logs insight-dashboard \| grep hw_jpeg` 看原因，`docker restart insight-dashboard` 重试 |
| `elements.nvjpegenc: false` | NVIDIA 运行时未注入插件：`docker inspect insight-dashboard --format '{{.HostConfig.Runtime}}'` 应为 `nvidia`；仍异常则报障 |
| 设备为 Orin Nano | 无此硬件，软件编码属预期 |

即使回退软件编码，功能不受影响，只是 CPU 变高。

### 6.7 手部叠加骨架闪烁

已知现象（检测偶发丢手 + 骨架未与图像帧做时间戳配对），**不影响录制数据**
——叠加只作用于显示画面，bag 里保存的是原始图像。

### 6.8 屏幕看板（--jetson）白屏

- 先用其他电脑的浏览器访问同一地址：能打开说明后端正常，只是本机窗口问题；
- SSH 登录执行时需指定显示器：`DISPLAY=:0 ./scripts/run_dashboard.sh --jetson`；
- 仍白屏：`docker restart insight-dashboard` 后重试。

### 6.9 时间不对（录制目录名/日志时间差 8 小时）

设备时区应为 `Asia/Shanghai`（容器内已配置）。若目录名时间不对，检查设备
系统时间 `date`；多机采集前确认各设备时间一致（影响数据对齐）。

---

## 7. 报障信息收集

报障时执行以下命令，把生成的文件发给我们：

```bash
{
  date
  cat /etc/nv_tegra_release | head -1
  sysctl net.core.rmem_max
  docker ps -a | grep insight
  ip -4 -br addr | grep 169.254
  curl -s localhost:8765/api/images/capabilities
  curl -s localhost:8765/api/cameras
  docker logs insight-dashboard --since 30m 2>&1 | tail -100
} > /tmp/insight_diag_$(date +%Y%m%d_%H%M%S).txt 2>&1
ls /tmp/insight_diag_*.txt
```

若问题与某次录制有关，同时附上该 bag 的检查结果：

```bash
docker exec insight-dashboard python3 scripts/check_bag.py rosbags/<目录名> > /tmp/insight_bag_check.txt
```
