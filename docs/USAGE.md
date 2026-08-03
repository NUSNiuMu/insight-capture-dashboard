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
                                ├─ 实时画面 + 3D 全局重定位轨迹（同一页）
                                ├─ rosbag 数据录制
                                └─ 完整性校验 / 评分
```

- **服务随开机自启**：容器 `restart: unless-stopped`，断电重启后自动恢复，
  正常情况下**无需任何手动启动操作**；设备与相机一起上电时，系统会在开机后
  **自动重启一次全部相机**（相机比主机先启动完成会处于错误的网络状态，
  重启一次让它们在主机链路就绪后重新连接，属预期行为）——从上电到画面
  就绪约 2-3 分钟；
- **运行中不支持热插拔**：这是开机自愈之外的另一种情况——后端/前端已经
  启动、系统正常运行之后，如果中途把某台相机的网线拔掉再插回去，相机
  重新连上并不会让画面自动恢复——必须等相机重新连上后，手动重启一次后端
  （`docker restart insight-dashboard` 或重跑 `./scripts/run_dashboard.sh`）
  画面才会回来，详见 §6.2；
- 所有数据（录制、配置、结果）保存在设备的 `rosbags/`、`config/`、`outputs/`
  目录，软件升级不会触碰它们。

---

## 2. 启动与访问

服务开机自启，通常不需要任何操作、直接访问即可（见 §2.2）。需要手动确认/重启服务时：

### 2.1 手动启动 / 重启

**情形 A：不在 Jetson 本机操作（自己电脑远程访问，或 Jetson 没接显示器）**

```bash
./scripts/run_dashboard.sh             # 启动或确认后端在跑；Ctrl-C 停止前台跟随
./scripts/run_dashboard.sh --logs      # 同时跟随后端日志
```

脚本自带健康检查与自愈：等待服务就绪、等待全部相机出数据；若检测到
"相机网口已连接但迟迟无数据"（开机时序问题）会自动重启一次后端。可随时
重复执行，幂等。停止服务：`docker compose down`（在部署目录下执行）。

**情形 B：就在 Jetson 本机操作，且接了显示器（要在设备屏幕上看全屏看板）**

```bash
./scripts/run_dashboard.sh --jetson
```

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
| `/` 或 `/3d` | 三路相机实时画面 + 3D 全局建图/重定位轨迹（同一页，2026-07 起合并） |
| `/recording` | rosbag 录制：topic 发现、勾选、开始/停止 |
| `/bags` | 本地录制列表：大小/时长/完整性/评分/优化状态，可删除 |
| `/scoring` | 轨迹评分 + 录制完整性验证 |
| `/handpose` | 从已有 rosbag 离线提取手部 3D 关键点并按时间轴查看 |
| `/optimization` | COLMAP 轨迹优化：对录制的彩色图像做三维重建并与 VIO 轨迹对齐 |
| `/settings` | 手势录制（默认关闭）、Stick-figure 模式全局开关；逐相机开关手部叠加和有效校准的夹爪追踪；Avatar 模型选择 |

> 旧地址 `/images` 现在会自动跳转到 `/3d`（画面已合并进 Spatial 视图，收藏的旧链接不用改）。

> 进入 `/3d` 后场景会先就绪，随后依次接通三路画面、轨迹和模型；模型加载期间不会显示占位物。

**Stick-figure 模式**（`/settings` 页开关）：开启后 3D 场景不再加载相机模型，改用三个角色配色的大圆点表示头/双手位置，同时叠加手臂骨骼与手部关键点骨架线（手部形状来自手部检测，手臂由固定臂长的 IK 解算合成，是合理近似而非真实追踪）。适合需要直观看操作者身体姿态（手臂弯曲、手指开合）的场景；开关状态影响所有观看者，翻转后下个刷新周期自动生效，无需刷新页面。

**手势录制**（`/settings` 页开关）：默认关闭。开启后双手点赞保持动作可以开始或停止由手势创建的录制；关闭会立即停止识别新的手势，但不会停止已经进行中的网页手动录制或录制任务。该开关与其他 Settings 开关一样在当前后端进程内即时生效，后端重启后恢复设备 profile 的默认关闭状态。

**Hand pose**（`/handpose` 页）：在页面内选择 `rosbags/` 中已有的录制，点击
**Extract hand pose**。任务使用 WiLoR 直接读取原录制，不会把 bag 复制到功能
目录；相机坐标结果保存为
`outputs/handpose/<bag 名>/wilor/result.json`。所需 CUDA/PyTorch 推理运行时和
固定版本权重已包含在 Dashboard 镜像中，设备离线时也可以提取。提取过程会保持
双手时序身份、过滤重复检测，并用 One-Euro Filter 降低 3D 关键点抖动。完成后
可拖动 3D 视图旋转、滚轮缩放、拖动时间轴，并点击关键点查看坐标。提取期间的
进度按目标图像 topic 已处理帧数除以 rosbag 记录的该 topic 总帧数计算，不使用
耗时或动画估算。

**UMI 训练数据导出**：打开 `/umi-dataset`，将每次完整示教对应的 rosbag 勾选为
episode，选择单臂 A、单臂 B 或双臂采集布局和训练图像分辨率后
点击 **Build UMI outputs**。每个选中的 rosbag 会独立处理并自动保存为
`outputs/umi_datasets/<rosbag 名>_umi/`，不需要再点击下载。
**Original resolution** 保留每路相机在 rosbag 中的原始宽高，不进行缩放；图像只转换为
UMI 需要的 RGB 三通道，Zarr 使用无损压缩。`224 × 224` 和 `384 × 384` 会先固定裁剪
水平居中、底部对齐的最大方形操作区，再等比缩放为训练副本，不会把 portrait 图像直接
拉伸成正方形。导出的 manifest 会记录每路相机的 `camera_crop_boxes_xywh`。
后台会在 recorder timeline 上把所选图像、pose、TCP 外参与夹爪二维码检测统一对齐到
20 Hz。每个输出目录包含：

- `<rosbag 名>_umi.zarr.zip`：可由官方 `UmiDataset` 直接读取的数据集；
- `<rosbag 名>_umi.umi.yaml`：按所选布局生成 `shape_meta`；单臂动作维度为 10，双臂为 20。
  复制到 UMI 仓库的
  `diffusion_policy/config/task/` 并修改 `dataset_path` 后即可用于训练；
- `<rosbag 名>_umi.manifest.json`：episode、帧数、同步偏差和夹爪检测率等质量摘要。

原始分辨率模式允许多路相机使用不同尺寸，并会把各自的 `[C,H,W]` 写入训练配置。
批量处理时某个 rosbag 导出失败不会阻止后续 rosbag，页面会逐包显示设备端保存路径或
失败原因。批处理结束后，因 VIO 连续性、有效帧数、图像解码或夹爪检测等录制数据质量
问题而无法生成任何有效 episode 的源 rosbag 会自动删除；至少生成一个有效 episode 的
rosbag 会保留。相机布局、标定、权限、磁盘或程序异常不会触发删除，避免误删可恢复数据。
原始分辨率会显著增加数据集大小、训练 I/O 和显存占用；rosbag 本身若记录的是 JPEG
compressed topic，导出只做解码，不会再增加一次有损压缩，但无法恢复采集时已经丢失的细节。

单臂布局只输出所选 Insight3 的 `camera0` 和 `robot0`，pose 来自该相机原生的
`/<namespace>/camera/vio_100hz`，每个 episode 使用自己的 VIO 坐标系；输入 bag 必须同时
包含图像和 VIO topic，且该夹爪已完成开合标定。双臂布局的顺序为右腕 `camera0`、左腕
`camera1`、头部 `camera2`，机器人顺序为右手 `robot0`、左手 `robot1`，并使用左右
`/insight_global/.../pose` 保证统一坐标系。`robot*_gripper_width` 使用夹爪标定得到的真实
开口宽度，单位为米，并记录在 Zarr 根属性中。默认模式下每条 rosbag 固定对应一个
episode；20 Hz 相邻 TCP 位置变化超过 5 cm、姿态变化超过 45°或 VIO pose 间隔超过
100 ms 时拒绝整条 rosbag，禁止跨越重定位、跟踪丢失或坐标重置插值。通过质量门的事件
计数写入 manifest。

需要在一条长录制中连续完成多次示教时，可在 UMI 页面选择 **Auto-split long recording
at pauses**。每一小节结束后保持 TCP 和夹爪静止约 1 秒；检测阈值为连续静止至少 0.8 秒。
导出器会同时检查 20 Hz 下的
平移速度（默认不超过 0.02 m/s）、旋转速度（不超过 10°/s）和夹爪宽度变化速度
（不超过 0.005 m/s），并在静止段中点建立 episode 边界。最多 0.15 秒的单帧检测毛刺
会被忽略。

自动切分不要求每次回到同一个绝对位置。每个 episode 单独写入自身的
`demo_start_pose`，训练配置继续使用 `pose_repr: relative`，因此前一小节积累的缓慢 VIO
漂移不会作为下一小节的绝对参考。若某个候选小节跨越 VIO 突跳、跟踪空洞或图像同步
异常，只丢弃该小节，其他完整小节仍会导出；若坐标重置恰好发生在静止段内，边界会
对齐到重置时刻，重置前后的小节都可保留。没有检测到足够长停顿时，整条长录制仍作为
一个 episode；因此采集时应有意停稳约 1 秒。

**单路离线夹爪诊断**：底层 `gripper_extract.py` 仍可单独运行，用于检查某一路
Insight3 图像中的二维码检测质量。

底层 `gripper_extract.py` 直接读取 rosbag 图像，逐帧检测 UMI 夹爪的 ArUco ID 1/0。
结果默认保存到
`outputs/gripper/<bag 名>/<相机名>.json`，包含 recorder/header 纳秒时间戳、
左右 marker 中心、像素距离和归一化开合度（`0=闭合，1=张开`）。例如：

```bash
docker exec -w /workspaces/insight_capture insight-dashboard \
  /entrypoint.sh python3 scripts/gripper_extract.py \
  rosbags/insight3_a_left_20260803_115721 --camera insight3_a
```

提取器默认从 `config/gripper_calibration.json` 按相机名读取全开/全闭像素距离。UMI
数据集导出还要求每个夹爪提供实测的 `width_calibration`，并按标定点进行分段插值，输出
单位为米。例如（数值仅展示格式，必须替换为实测值）：

```json
{
  "insight3_a": {
    "open_px": 247.28,
    "closed_px": 54.55,
    "width_calibration": [
      {"distance_px": 54.55, "width_m": 0.0},
      {"distance_px": 140.0, "width_m": 0.038},
      {"distance_px": 247.28, "width_m": 0.083}
    ]
  }
}
```

不要直接把归一化开合比例乘以最大宽度；如果机械结构或成像关系不是线性的，应增加中间
实测点。`gripper_calibrate.py` 可通过 `--closed-width-m` 和 `--open-width-m` 写入端点，
中间点可在标定 JSON 中补充。
尚未标定时仍会输出 marker 中心和 `distance_px`，但 `opening` 为 `null`；需要强制
要求开合度时加 `--require-calibration`。也可用
`--open-px <值> --closed-px <值>` 临时覆盖标定。多图像 topic 的 bag 应显式传
`--topic`，具体参数见 `scripts/gripper_extract.py --help`。


### 3.1 标准采集流程

1. 打开 `/3d`，确认三路画面都在动（面板无 stale 灰标）；
2. `/recording` → `Refresh Topics` → 勾选要录的 topic（支持按相机整组勾选）→ `Start`；
3. 采集完成 → `Stop`,等待录包流程结束；
4. `/umi-dataset` 选择一条或多条完整示教，导出 Zarr、训练配置与 manifest；
5. **校验数据完整性并打分**：打开 `/scoring` 页，选中刚录的 bag，点
   **Scoring**（一个按钮同时做两件事：先跑完整性校验，报告先出来；随后
   不论完整性结果如何都自动接着跑轨迹评分）。

   - **完整性结果**：逐 topic 一行，格式为「消息条数 · 实测频率/标称频率Hz ·
     丢失 X%」，行首 `ok`/`FAIL` 标色。顶部结论只有两种：绿色
     **Complete**（所有 topic 丢帧都在阈值内）或红色 **Incomplete**
     （列出具体哪些 topic 丢帧，按 §6.3 排查）。结果会持久保存，`/bags`
     列表中该 bag 会带上徽章 `complete` / `incomplete`（从没验证过的
     显示 `unverified`）。
   - **轨迹评分**：每台相机一张卡片，给出 0-100 分和一个质量等级标签，
     用于横向比较不同录制/相机的相对好坏；评分完成后 `/bags` 列表该
     bag 带上 `scored` 徽章（未跑过显示 `unscored`）。评分具体如何计算
     不对外说明，仅看结果即可。

也可以在 Insight9 画面内同时做双手点赞：两只手均保持“拇指向上、其余
四指握拳”至少 0.8 秒会使用服务器默认 topics 开始录制。触发后先解除手势
至少 2 秒，再次保持双手点赞 0.8 秒会停止该段录制。手势不会停止网页手动
开始的录制；磁盘剩余低于 10% 或上一段仍在合并时也不会自动开始。Recording
页顶部的 `gesture` 标签显示 armed、保持、释放和录制状态。

命令行等价方式（脚本化/无浏览器时）：

```bash
docker exec insight-dashboard python3 scripts/check_bag.py                # 最新一份就可以
docker exec insight-dashboard python3 scripts/check_bag.py rosbags/<目录名>
docker exec insight-dashboard python3 scripts/check_bag.py --fast rosbags/<目录名>  # 仅元数据的快速估计；不作为完整性判定
```


### 3.3 磁盘管理

三路全录约 **1.2GB/分钟**，录制前确认空间：

```bash
df -h /                              # 剩余空间
du -sh rosbags/* | sort -h | tail    # 各录制占用
```

删除：`/bags` 页面操作，或直接删除 `rosbags/` 下对应目录（确认已拷贝/不需要后）。

---



### 4. 轨迹优化（COLMAP）

`/optimization` 页选择 bag 和相机后启动（目前只支持insight9 彩色图）。设备端全流程运行（GPU 加速特征
提取/匹配），一段 1-2 分钟的录制约需 10-15 分钟出结果，期间页面实时显示
进度和 COLMAP 日志。**优化运行时避免同时录制**（两者都吃满资源）。

结果不理想时的判断依据：

- **"No good initial image pair found" 反复出现、注册率 <10%** →
  录制内容不适合三维重建：需要相机有充分的平移运动（不能只旋转/静止）、
  场景纹理丰富、光照稳定。换一段运动更充分的录制重跑；
- 正常成功的参考值：注册率 >50%，Sim3 对齐匹配点数 ≥ 几十个。

---

## 5. 软件升级

升级只需要一个新的镜像压缩包（`insight-dashboard-vX.Y.Z.tar.gz`），
在部署目录执行：

```bash
./update.sh insight-dashboard-vX.Y.Z.tar.gz
```

录制数据、标定结果、配置全部保留。回滚：`./update.sh --rollback vX.Y.Z`。

完整的升级排查步骤、回滚细节、全新设备部署方式见 [DEPLOYMENT.md](DEPLOYMENT.md)。

---

## 6. 常见问题排查

> 任何异常先跑通用三板斧（先怀疑相机是否掉线：查看对应网卡是否存在；
> 相机重新连上后画面仍不恢复，重启一次后端）：
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

**症状**：`/scoring` 页点 Scoring 后，完整性结果面板显示红色 Incomplete（或
`/bags` 列表出现红色 `incomplete` 徽章、命令行 `check_bag.py` 报 FAIL）。

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
   然后重录一段用 `check_bag.py` 复验；
2. **只有高频小消息 topic（400Hz IMU 等）分散丢几个百分点、image/camera_info
   完好** → 与上一条是不同的内核层：不是 socket 接收缓冲（那个已经够大），是
   NAPI 每核 backlog 队列太浅——这台机型相机 USB 网口的中断全部落在同一个
   CPU 核上（`cat /proc/interrupts | grep xhci` 只有一列非零可确认），该核
   默认 1000 条的 backlog 队列在多相机流量突发时会溢出，包在到达任何
   DDS socket 之前就已经在内核网络层被丢弃，`ip -s link show <相机对应网口>`
   的 `dropped` 计数会在录制窗口内持续增长。验证与恢复：
   ```bash
   sysctl net.core.netdev_max_backlog   # 正常应 >= 8192；默认值 1000 即命中
   cat /proc/net/softnet_stat | head -1 # 第2列(16进制)=丢包数在录制前后应几乎不变
   ```
   恢复（与上一条同一个 `/etc/sysctl.d/99-dds-rx-buffers.conf` 文件，
   `scripts/host_setup.sh` 会一并写入两项设置，重跑一次即可持久化）：
   ```bash
   sudo sysctl -w net.core.netdev_max_backlog=8192   # 立即生效，未持久化
   ./scripts/host_setup.sh                            # 持久化进 99-dds-rx-buffers.conf
   ```
   然后重录一段用 `check_bag.py` 复验（此问题实测 IMU 丢帧
   0.47-0.94%，修复后为 0.0%，验证于 2026-07-14）；
3. **所有 topic 在同一时间段一起断** → 录制期间设备被其他任务抢占，
   `docker stats insight-dashboard` 观察 CPU；录制时避免同时跑评分/优化任务；
4. **某台相机自己的全部 topic（含 IMU/VIO）同时断** → 相机侧停顿，
   与主机无关；复现请记录相机名与时间点后报障；
5. **磁盘写满**：见 §3.2。

### 6.4 录制无法开始 / 停止异常

- 先看状态：`curl -s localhost:8765/api/recording/status`；
- `Start` 无反应：点一次 `Refresh Topics` 再试（相机刚重启过时 topic 列表会过期）；
- 注意：录制进行中**不要**执行 `docker restart`，会中断录制。
  `run_dashboard.sh` 已内置保护（检测到录制中不重启），手动 docker 命令没有。

### 6.5 画面卡顿 / 帧率低

- 相机卡片上的数字是浏览器实际呈现帧率；鼠标悬停可查看
  `source → processed → IPC → appsrc → encoded → browser received/decoded/presented`
  的逐段速率和累计丢帧。
- 后端原始统计也可通过
  `curl -s localhost:8765/api/cameras | python3 -m json.tool` 查看。每路相机的
  `webrtc_stats` 包含主进程覆盖数、worker 限频数、`appsrc` 失败数和编码速率。
- `source` 已低于相机标称帧率时按 §6.2 查相机链路；`encoded` 正常但浏览器
  `received/decoded/presented` 下降时，检查浏览器、网络和 3D/GPU 合成负载。
- 录制期间 WebRTC 预览会主动降帧，录制数据本身仍保持相机原生帧率。

### 6.6 CPU 占用异常高（硬件编码回退）

正常三路相机约占一个核（`docker stats insight-dashboard` 的 CPU% ≈100%）。
明显偏高时检查硬件编码是否在用：

```bash
curl -s localhost:8765/api/images/capabilities | python3 -m json.tool
```

`active_path` 应为 `jpeg-hardware-nvjpeg`（当前该诊断只能用上面这条命令查，
`/settings` 页面已经没有对应的图像管线诊断卡片了）。

| 现象 | 处理 |
|---|---|
| `hw_jpeg.disabled` 里有条目 | `docker logs insight-dashboard \| grep hw_jpeg` 看原因，`docker restart insight-dashboard` 重试 |
| `elements.nvjpegenc: false` | NVIDIA 运行时未注入插件：`docker inspect insight-dashboard --format '{{.HostConfig.Runtime}}'` 应为 `nvidia`；仍异常则报障 |
| 设备为 Orin Nano | 无此硬件，软件编码属预期 |

即使回退软件编码，功能不受影响，只是 CPU 变高。

### 6.7 手部叠加/骨架闪烁

- **2D 图像叠加**（画面里的手部关键点线）：已做时间戳同步窗口门控，关键点帧
  与图像帧差距过大时直接跳过绘制而不是画错位骨架，正常情况下不应有明显闪烁；
- **3D 场景骨架**（Stick-figure 模式或普通模式下的手臂/手部骨架线）：手部检测
  本身逐帧偶有漏检，已加入"连续命中几次才出现、短暂断检保持上一姿态"的
  防抖逻辑，大幅减少闪烁，但检测持续丢失较长时间时骨架仍会消失（预期行为，
  不是 bug）；
- 以上均**不影响录制数据**——叠加/骨架只作用于显示画面，bag 里保存的是
  原始图像和原始检测数据；
- **已知遗留问题**：手部检测的左右手判断本身不总是稳定，偶尔可能出现骨架
  挂到镜像手上的情况（即左手数据显示在右手位置，反之亦然）；不影响图像
  或录制数据本身。

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
