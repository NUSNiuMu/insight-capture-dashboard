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
- **运行中掉线可自愈，但录制优先**：非录制状态下，某路相机连续 15 秒无帧且
  USB 网口仍在线时，后端 watchdog 会退出并由 Docker 自动重建 DDS participant；
  拔插后通常等待几十秒即可恢复。录制期间不会为了单路掉线重启后端，以免中断
  其他相机；应先停止当前录制，再按 §6.2 手动恢复；
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
| `/` | Session / Take 列表、quick QC、人工接收/作废结果与异常摘要 |
| `/3d` | 采后三路相机回放 + 3D 全局建图/重定位轨迹；打开后才按需启用实时预览 |
| `/recording` | 维护用 rosbag 录制页；现场主流程使用离线语音命令 |
| `/bags` | 本地录制列表：大小/时长/消息与 topic 数、完整性/评分状态，可删除 |
| `/umi-dataset` | 标准 LeRobot v3 或 Legacy UMI Zarr 训练数据导出 |
| `/scoring` | 轨迹评分 + 录制完整性验证 |
| `/handpose` | 从已有 rosbag 离线提取手部 3D 关键点并按时间轴查看 |
| `/optimization` | COLMAP 轨迹优化：对录制的彩色图像做三维重建并与 VIO 轨迹对齐 |
| `/settings` | 逐相机手部叠加、夹爪追踪及 Insight3 mask 等高级设置 |

Recording 页的 **Recording folder → Browse...** 可在录制空闲时切换保存目录。弹窗只显示
Dashboard 容器已经挂载的录制盘与 NVMe fallback 范围，不会暴露整台设备文件系统；浏览器所在
电脑的普通本地目录若未挂载进容器也不会出现。确认目录时后端会执行实际写入和 `fsync` 探测，
成功后只改变后续录制的写入目标，不会隐藏或搬动旧目录中的 rosbag。Bags 页可在 **All
directories** 与 **Current recording directory** 间筛选；回放、评分、Hand pose、优化和
数据集导出通过稳定 bag ID 定位所选录制，不依赖当前写入目录。本次写入目录选择保留到
Dashboard 后端重启，重启后重新采用 `INSIGHT_ROSBAG_DIR`、必需挂载源检查与 fallback 配置。

> 旧地址 `/images` 现在会自动跳转到 `/3d`（画面已合并进 Spatial 视图，收藏的旧链接不用改）。

> 进入 `/3d` 后场景会先就绪，随后依次接通三路画面、轨迹和模型；模型加载期间不会显示占位物。

**Hand pose**（`/handpose` 页）：在页面内选择 `rosbags/` 中已有的录制，点击
**Extract hand pose**。任务使用 WiLoR 直接读取原录制，不会把 bag 复制到功能
目录；相机坐标结果保存为
`outputs/handpose/<bag 名>/wilor/result.json`。所需 CUDA/PyTorch 推理运行时和
固定版本权重已包含在 Dashboard 镜像中，设备离线时也可以提取。提取过程会保持
双手时序身份、过滤重复检测，并用 One-Euro Filter 降低 3D 关键点抖动。完成后
可拖动 3D 视图旋转、滚轮缩放、拖动时间轴，并点击关键点查看坐标。提取期间的
进度按目标图像 topic 已处理帧数除以 rosbag 记录的该 topic 总帧数计算，不使用
耗时或动画估算。

**训练数据集导出**：打开 `/umi-dataset`，将每次完整示教对应的 rosbag 勾选为
episode 并填写英文任务指令。点击 **Inspect and build LeRobot dataset** 后，后端先从
腕部图像抽样检测 UMI 夹爪的 ArUco ID 1/0：重复检测到双标记时走 UMI 夹爪路线；两路
腕部图像均未检测到双标记时走三视角 Ego 手姿路线，并只在 Insight9 头部图像上运行
WiLoR。单臂/双臂布局、自动停顿分段和图像缩放仅用于夹爪路线；手姿路线固定保留三路
原图，并把整条 rosbag 作为任务文本对应的单 episode、单动作段。默认输出目录为：
`outputs/lerobot_datasets/<rosbag 名>_lerobot/`。每个选中的 rosbag 会独立处理并保存在设备上。
页面当前默认保留 **Original resolution**；需要 π0.5 固定输入时可显式选择 224×224
或 384×384 的下部操作区夹爪训练副本。

LeRobot 输出包含：

- `data/chunk-000/file-000.parquet`：逐帧 `observation.state`、`action` / `actions`、validity mask、
  timestamp、episode/frame/task index；
- `videos/observation.images.*/chunk-000/file-000.mp4`：所选相机的同步 H.264/yuv420p
  视频；单臂键为 `right_wrist_0_rgb`，双臂三相机键为 `base_0_rgb`、
  `left_wrist_0_rgb`、`right_wrist_0_rgb`；
- `meta/info.json`、`stats.json`、`tasks.parquet`、`modality.json` 和 episode parquet：
  LeRobot v3 schema、归一化统计、语言任务、语义切片和视频/数据分片索引；
- `meta/manifest.json`：设备端导出摘要和数据来源。

夹爪路线的单臂状态和动作均为 10 维 `xyz + rotation_6d + gripper`；双臂为 20 维并固定使用
`[left_10d, right_10d]`。位置是米制绝对 EE 位置，rotation 6D 是旋转矩阵前两行，
夹爪是经实测标定的物理开口宽度（米），不是开口角。所有数值均为有限 float32；缺失或
非有限输入使用有限占位，并在逐维 `observation.state_valid` / `action_valid` 中标为 false。
`action[t]` 是同一 episode 的下一帧绝对状态 `T[t+1]`，不是
`inv(T[t]) @ T[t+1]`。末帧 action 重复末状态作有限占位，但其 `action_valid` 全 false，
训练损失和归一化统计都必须忽略无效维度。

`action` 是官方 LeRobot v3 loader 的标准键；`actions` 是完全相同的 OpenPI 默认序列键，
对应 validity 为 `actions_valid`。夹爪宽度若出现超过 30 mm/frame 的不连续跳变，导出会
保留原始有限测量值并把该侧宽度维度标为无效；孤立尖峰恢复后的下一帧仍可有效。

无夹爪手姿路线输出左右手各 54 维（腕部 `xyz + rotation_6d` 加 45 维 MANO 姿态），
同时写入 2D/3D 关键点、显式 validity mask 和三路原始分辨率视频；详细交付约束见
`docs/EGO_LEROBOT_EXPORT.md`。

224×224 模式会对原始 640×544 画面执行固定 ROI `[x=0, y=96, width=544,
height=544]` 后再缩放，不能在实机推理时直接拉伸完整画面。NV12 腕部画面会保留完整
Y/UV 色度并转换成三通道 RGB，不能仅使用亮度平面。Parquet 额外保存统一的
`observation.timestamp_ns`、每路
`*_timestamp_ns` 真实 rosbag 时间戳及每路 `*_valid`；同步缺失或超出容差的图像必须按
validity 忽略，不能当成有效观测。

训练侧取得未来绝对 action chunk 后，应在 normalization 之前分别用当前观测 `T0` 计算
`A[k] = inv(T0) @ T[k+1]`；不得计算相邻未来帧之间的 sequential delta。夹爪不参与
SE(3) 运算，保留未来时刻的绝对目标宽度。双臂应对左右臂独立执行该转换。

所选的每个 Insight3 夹爪都必须在 `config/gripper_calibration.json` 中提供实测的
`width_calibration`；因此双臂导出要求 A、B 两侧均完成米制开口宽度标定。配置缺失会使
该 bag 保留并报告配置错误，不会输出伪造的宽度或误删源数据。

OpenPI π0.5 训练配置应把 LeRobot 相机 key、任务文本和 20 维状态映射到模型输入，并在
data transform 中将绝对下一状态转换为部署机器人需要的相对动作。训练前按相同 validity
过滤规则运行 OpenPI 的 `compute_norm_stats.py`，不要直接复用其他机器人或 HiFi-UMI
设备的归一化统计。

**Legacy UMI Zarr**：需要继续使用官方 UMI Diffusion Policy 时，在 Dataset format 中
选择 **Legacy UMI · Zarr replay buffer**。输出仍保存到
`outputs/umi_datasets/<rosbag 名>_umi/`，包含：

- `<rosbag 名>_umi.zarr.zip`：可由官方 `UmiDataset` 直接读取的数据集；
- `<rosbag 名>_umi.umi.yaml`：按所选布局生成 `shape_meta`；单臂动作维度为 10，双臂为 20。
  复制到 UMI 仓库的
  `diffusion_policy/config/task/` 并修改 `dataset_path` 后即可用于训练；
- `<rosbag 名>_umi.manifest.json`：episode、帧数、同步偏差和夹爪检测率等质量摘要。

**Original resolution** 保留每路相机在 rosbag 中的原始宽高，不进行缩放；`224 × 224`
和 `384 × 384` 会先固定裁剪水平居中、底部对齐的最大方形操作区，再缩放为训练副本，
不会把 portrait 图像直接拉伸成正方形。UMI Zarr 图像使用无损压缩；LeRobot 视频使用
H.264 CRF 18/yuv420p。导出的 metadata 会记录每路相机的 crop box 和编码参数。
后台会在 recorder timeline 上把所选图像、pose、TCP 外参与夹爪二维码检测统一对齐到
20 Hz。

原始分辨率模式允许多路相机使用不同尺寸；UMI 会把各自的 `[C,H,W]` 写入训练配置，
LeRobot 会把各自的 `[H,W,C]` 和视频参数写入 `info.json`。
批量处理时某个 rosbag 导出失败不会阻止后续 rosbag，页面会逐包显示设备端保存路径或
失败原因。批处理结束后，因 VIO 连续性、有效帧数、图像解码或夹爪检测等录制数据质量
问题而无法生成任何有效 episode 的源 rosbag 会重命名为 `fail_<原名>` 并从 Dataset
页面隐藏，但仍保留在 `rosbags/` 中供 Bags 页面检查或手工处理；若目标名称已存在，使用
`fail_2_<原名>` 等递增名称避免覆盖。至少生成一个有效 episode 的 rosbag 会保留原名。
相机布局、标定、权限、磁盘或程序异常不会改名，避免把可恢复问题误标为坏数据。
原始分辨率会显著增加数据集大小、训练 I/O 和显存占用。UMI Zarr 对 JPEG compressed
topic 只解码后无损保存；LeRobot 会统一转码为 H.264，因此会增加一次受控的有损编码。
两种格式都无法恢复采集时已经丢失的细节。

单臂布局的 pose 来自所选 Insight3 原生的
`/<namespace>/camera/vio_100hz`，每个 episode 使用自己的 VIO 坐标系；输入 bag 必须同时
包含图像和 VIO topic，且该夹爪已完成开合标定。UMI 输出 `camera0` / `robot0`；LeRobot
仍使用 20 维双手 schema，并 mask 缺失侧。双臂布局使用右腕、左腕、头部三路图像，并用
左右 `/insight_global/.../pose` 保证统一坐标系。夹爪开口宽度来自实测标定，单位为米，
记录在 UMI Zarr 根属性或 LeRobot `modality.json` 中。默认模式下每条 rosbag 固定对应一个
episode；位置门先找出超过 5 cm 的 TCP 步长，再要求该步相对前后局部速度的位移创新量
也超过 5 cm 才判定跳变，因此保留连续高速运动，同时拒绝坐标重置产生的速度脉冲。
姿态变化超过 45°或 VIO pose 间隔超过 100 ms 时仍拒绝整条 rosbag，禁止跨越重定位、
跟踪丢失或坐标重置插值。通过质量门的事件计数写入 manifest。
录制页会默认同时勾选每路相机的原始 `vio_100hz` 和配置的 dashboard/global pose，确保
后续 UMI 单臂导出不依赖用户手动补选原始 VIO。

需要在一条长录制中连续完成多次示教时，可在 Dataset 页面选择 **Auto-split long recording
at pauses**。每一小节结束后保持 TCP 和夹爪静止约 1 秒；检测阈值为连续静止至少 0.8 秒。
导出器会同时检查 20 Hz 下的
平移速度（默认不超过 0.02 m/s）、旋转速度（不超过 10°/s）和夹爪宽度变化速度
（不超过 0.005 m/s），并在静止段中点建立 episode 边界。最多 0.15 秒的单帧检测毛刺
会被忽略。

自动切分不要求每次回到同一个绝对位置。UMI 为每个 episode 单独写入自身的
`demo_start_pose`，训练配置继续使用 `pose_repr: relative`；LeRobot 保留采集坐标系的绝对
下一状态，由 π0.5 data transform 按 episode 转为所需相对动作。若某个候选小节跨越 VIO 突跳、跟踪空洞或图像同步
异常，只丢弃该小节，其他完整小节仍会导出；若坐标重置恰好发生在静止段内，边界会
对齐到重置时刻，重置前后的小节都可保留。没有检测到足够长停顿时，整条长录制仍作为
一个 episode；因此采集时应有意停稳约 1 秒。

**单路离线夹爪诊断**：底层 gripper extraction 模块可单独运行，用于检查某一路
Insight3 图像中的二维码检测质量。

底层模块直接读取 rosbag 图像，逐帧检测 UMI 夹爪的 ArUco ID 1/0。
结果默认保存到
`outputs/gripper/<bag 名>/<相机名>.json`，包含 recorder/header 纳秒时间戳、
左右 marker 中心、像素距离和归一化开合度（`0=闭合，1=张开`）。例如：

```bash
docker exec -w /workspaces/insight_capture insight-dashboard \
  /entrypoint.sh python3 -m insight_capture.postprocess.gripper.extraction \
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
实测点。`python3 -m insight_capture.perception.gripper.calibration` 可通过
`--closed-width-m` 和 `--open-width-m` 写入端点，
中间点可在标定 JSON 中补充。
尚未标定时仍会输出 marker 中心和 `distance_px`，但 `opening` 为 `null`；需要强制
要求开合度时加 `--require-calibration`。也可用
`--open-px <值> --closed-px <值>` 临时覆盖标定。多图像 topic 的 bag 应显式传
`--topic`，具体参数见
`python3 -m insight_capture.postprocess.gripper.extraction --help`。


### 3.1 无屏语音采集流程

1. 当前唯一的 Task 是“叠杯子”。首次采集直接使用默认 Task，也可说“开始任务叠杯子”
   确认；同一 Task 已经进行时不会重置当前批次；
2. 说“系统状态”，确认三相机、定位、存储和必要录制数据流通过 Preflight；
3. 说“开始录制”；系统在当前 Session 中分配递增的 Take，并在 recorder 真正开始后播报确认；
4. 采集完成后说“停止录制”，等待 quick QC 播报；操作失败时说“本条作废”，只标记
   invalid，不删除原始 MCAP；
5. 随时说“当前任务多少条”，系统会播报本批已录制、有效、作废和下一条编号；本批完成后
   说“结束当前任务”，下次开始叠杯子任务会创建新 Session；
6. `/umi-dataset` 选择一条或多条完整示教；默认导出 LeRobot v3，只有兼容旧
   Diffusion Policy 训练栈时才选择 Legacy UMI Zarr；
7. **校验数据完整性并打分**：打开 `/scoring` 页，选中刚录的 bag，点
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

设备可以直接说“开始任务叠杯子”“当前任务多少条”“结束当前任务”“开始录制”、
“录制校准模式”“停止录制”“开始校准”“检查相机”“系统状态”或“本条作废”，无需先说唤醒词；
这些固定短指令调用本机 Dashboard 并播放本地确认语音，
不依赖 OpenClaw、Gateway 或网络。
开始/停止录制在识别后会立即播报“初始化录制中，请稍等”/“正在结束录制”，待 rosbag 真正就绪或
排空后再播报最终结果。
需要自然语言操作时，单独说“宸境”并停顿 0.5 秒，听到“我在”后再说下一句话；这句话
固定交给 OpenClaw。这里的“开始校准”会清空旧地图和全局定位状态，开始新一轮在线校准。
当两路 Insight3 首次都完成全局定位时，音响会再播报一次“校准完成”。

“录制校准模式”与“开始校准”不同：前者直接开始一条 `vio_calibration_时间戳` MCAP，
只录 Insight3 A/B 的左右未校正图像、两路 CameraInfo、IMU、原生 VIO 诊断和
`/tf_static`，固定为 17 个 topic，不包含 `/insight_global/.../pose`，也不计入当前
Task/Take。开始前会逐路读取一帧原图；任一路只有 publisher 而没有 payload 时拒绝录制。
相机切到只发未校正图像后，网页校正预览为空属于预期现象，不表示相机断开；校准录制以
16 个非 latched topic 的发布者和四路原图实际 payload 为准。Dashboard 另用原生
`vio_100hz` 判断相机链路存活，不能因校正预览停止而重启。
完成后仍说“停止录制”。
叠杯等重复任务在每条录制停止后，应将左右手两台 Insight3 放回固定治具，然后说“检查相机”。
系统播报“开始检测”后进入 12 秒检测窗口；让头戴 Insight9 对准设置检测位时的工作区方向并
小范围缓慢扫动。系统同时比较两台 Insight3 的固定 Pose，并要求 Insight9 相对批次开始时冻结
的自然特征地图产生一条新闭环；闭环结果可保留 30 秒，闭环校正量
用于判断头部 VIO 是否漂移。结果按 PASS/复检/重校准分级并关联到最近一条 bag。首次部署
治具或开始新地图 session 时，由标定负责人确认校准正确后说“设置检测位”。详见
[叠杯批量数据采集标准作业规范](CUP_STACKING_DATA_COLLECTION_SOP.md)。
回复会从 USB 音响播出；服务
开始/停止属于有副作用的操作，必须明确说出；Dashboard 本身不占用麦克风，声卡只由
宿主机 voice service 使用。完整现场流程见 [CAPTURE_SOP.md](CAPTURE_SOP.md)。

检查宸境服务和 USB 声卡：

```bash
arecord -l
aplay -l
systemctl --user status insight-voice-control.service
journalctl --user -u insight-voice-control.service -n 100 --no-pager
```

完整安装、隐私边界和离线测试见 [OPENCLAW_VOICE.md](OPENCLAW_VOICE.md)。

命令行等价方式（脚本化/无浏览器时）：

```bash
docker exec insight-dashboard python3 scripts/check_bag.py                # 最新一份就可以
docker exec insight-dashboard python3 scripts/check_bag.py rosbags/<目录名>
docker exec insight-dashboard python3 scripts/check_bag.py --fast rosbags/<目录名>  # metadata/SQLite 快速聚合；不读取 CDR payload
```

`--fast` 对旧 SQLite bag 执行每 topic `COUNT/MIN/MAX(timestamp)`，对 MCAP 读取 metadata
（旧复合会话则汇总各 part）；它适合快速盘点，不包含录制期间的图像 header 连续性 live audit。
默认深检会顺序读取 payload 并检查 header 间隔，速度较慢但结论更精确。

`/bags` 的回放会先按 rosbag record timestamp 把已有图像与 Pose 预编码到统一 30 Hz
时间轴，再从缓存播放。不能用原始 `header.stamp` 做跨相机同步：Insight3 可能发布
Unix/NTP 时间，而 Insight9 可能发布设备启动时间；两者数值不在同一时钟域。

### 3.2 磁盘管理

占用取决于勾选的图像变体和录制时长；同时选择多路 raw/rectified/depth 流时可达到
每分钟数 GB，不能再用固定的“三路默认值”估算。录制前确认空间：

```bash
df -h /                              # 剩余空间
du -sh rosbags/* | sort -h | tail    # 各录制占用
```

删除：`/bags` 页面操作，或直接删除 `rosbags/` 下对应目录（确认已拷贝/不需要后）。
删除是永久操作；`rosbags/_staging/` 是中断录制恢复区，不要在录制、排空或恢复期间清理。

要直接录入 ext4 U 盘，在 Docker Compose 的 `.env` 设置：

```bash
INSIGHT_ROSBAG_HOST_DIR=/media/nvidia/INSIGHT_USB/rosbags
INSIGHT_ROSBAG_REQUIRED_SOURCE=/dev/sda1
```

然后重建 Dashboard 服务。启动录制前确认 `findmnt` 显示 U 盘为 `rw`，Recording 状态中的
`disk_space.path` 指向 `/mnt/insight-recordings`。FAT32 单文件 4 GiB 限制不适合该数据量。
服务启动时及每次开始录制前都会核对 `INSIGHT_ROSBAG_REQUIRED_SOURCE`，并在 `_staging`
执行一次写入/`fsync` 探测。挂载不匹配、只读或发生 I/O 错误时会自动写入本机 NVMe 的
`rosbags/`；只要 fallback 可写且空间充足，Preflight 会报告 warning 但允许录制。API 状态中的
`storage.using_fallback=true`，并由 `storage.active_path` 和 `storage.fallback_reason` 给出实际
路径与原因。需要严格禁止 fallback 的部署可将 `preflight.require_primary_storage` 设为
`true`。一次回退后本进程固定使用 NVMe；修复或更换
U 盘后重启 Dashboard，才会重新选择 U 盘。录制过程中断盘无法迁移已经写出的同一段数据，
应停止该段并在下一段录制前确认状态已回退。

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
固定版本的 SuperGlue 依赖已从日常升级包拆出；只有全新设备首次安装时需要额外提供
`insight-superglue-validation-25.04.tar.gz`，已有设备不会重复加载或传输。

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
3. **运行中拔插或单路停流**：未在录制时先等待约 30 秒；watchdog 会在链路在线且
   连续 15 秒无帧后重启后端。录制中 watchdog 不会动作，应停止录制，再执行
   `docker restart insight-dashboard`；仍无数据则断电重启该相机；
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
   net.ipv4.ipfrag_max_dist = 4096
   EOF
   sudo sysctl -p /etc/sysctl.d/99-dds-rx-buffers.conf
   docker restart insight-dashboard
   ```
   然后重录一段用 `check_bag.py` 复验；
2. **高频小消息或增加 raw image 后丢帧** → 与上一条是不同的内核层：不是
   socket 接收缓冲（那个已经够大），是 NAPI 每核 backlog 与单核处理能力不足——
   这台机型相机 USB 网口的中断全部落在 CPU0（`cat /proc/interrupts | grep xhci`
   只有一列非零可确认）。未配置 RPS 时，协议处理也全部留在 CPU0；多相机流量
   突发会让该核的 backlog 溢出，包在到达任何
   DDS socket 之前就已经在内核网络层被丢弃，`ip -s link show <相机对应网口>`
   的 `dropped` 计数会在录制窗口内持续增长。验证与恢复：
   ```bash
   sysctl net.core.netdev_max_backlog   # 正常应 >= 8192；默认值 1000 即命中
   cat /proc/net/softnet_stat           # 每行第2列(16进制)=丢包数，录制前后应不变
   for f in /sys/class/net/enx*/queues/rx-0/rps_cpus; do echo "$f: $(cat "$f")"; done
                                        # 相机 cdc_ncm 网卡不能是 00；6 核 Jetson 应为 3e
   ```
   恢复（`deploy/host_setup.sh` 会同时持久化 sysctl，安装并立即运行
   `insight-camera-network.service` 和相机 USB 重连时恢复 RPS 的 udev 规则）：
   ```bash
   sudo ./deploy/host_setup.sh
   ```
   然后重录一段用 `check_bag.py` 复验；
3. **softnet/网卡/UDP 都不丢，但多路 raw image 仍缺帧** → 检查 DDS 模式与 IP 分片重组：
   ```bash
   python3 tools/device_cli/looper_cli.py --device-base-url http://<相机IP> dds show
   sysctl net.ipv4.ipfrag_max_dist     # jetson-nx 应为 4096
   nstat -az IpReasmFails IpReasmTimeout UdpInErrors UdpRcvbufErrors
   ```
   每个新 bag 的 `recording_network_audit.json` 已保存这些计数在录制窗口内的增量，
   `recording_manifest.json` 则保存当时实际选择的 topic。`IpReasmFails` 大量增长说明
   大图 UDP 包已到主机、但在 DDS 之前重组失败。jetson-nx 应使用
   `config/cameras.json` 的 `camera_dds_type=cyclonedds`；开机恢复流程会幂等校正设备，
   `sudo ./deploy/host_setup.sh` 会持久化内核阈值。不要通过换 SSD 或提高 SQLite
   同步级别处理。保留失败 bag 与这两个 JSON 后报障；
4. **所有 topic 在同一时间段一起断** → 录制期间设备被其他任务抢占，
   `docker stats insight-dashboard` 观察 CPU；录制时避免同时跑评分/优化任务；
5. **某台相机自己的全部 topic（含 IMU/VIO）同时断** → 相机侧停顿，
   与主机无关；若 `recording_network_audit.json` 全部为零且原生 recorder 未报告 cache 丢失，
   同一相机多个 topic 在同一 header 时间点一起缺样同样属于发布端节拍缺口。复现请
   记录相机名与时间点后报障；
6. **磁盘写满**：见 §3.2。

### 6.4 录制无法开始 / 停止异常

- 先看状态：`curl -s localhost:8765/api/recording/status`；
- `Start` 无反应：点一次 `Refresh Topics` 再试（相机刚重启过时 topic 列表会过期）；
- 声控无反应：检查 `systemctl --user status insight-voice-control.service`、宿主机
  `arecord -L` 与 `journalctl --user -u insight-voice-control.service -n 100 --no-pager`；
- 声控识别成功但不开始：读取 `/api/preflight`；系统会拒绝相机 stale、定位未就绪、
  存储不可写/空间不足或必要 topics 缺失的录制；
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
- **3D 场景手部骨架**：手部检测
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

需要保留下次相机掉线前后的完整时间线时，在 Jetson 宿主机仓库目录常驻运行：

```bash
python3 tests/diagnostics/monitor_camera_failures.py
```

日志默认写入 `outputs/camera_diagnostics/camera-monitor-*.jsonl`，单文件达到 100 MiB
后自动分卷。脚本每秒只读检查 Dashboard 相机 API、三台相机 HTTP 和 USB 网卡计数，
每 30 秒保存完整快照；同时跟随内核 USB journal。`fault_started`/`fault_resolved`
记录异常起止，`cause` 给出跨层归因，重点区分：

- 多路 USB 同时断开：Hub、供电或 USB 控制器复位；
- 网卡和相机 HTTP 仍正常、有接收流量但没有图像：相机图像发布或 DDS 大包链路停滞；
- 网卡仍在但相机 HTTP 不通：相机启动、服务或 IP 链路异常；
- 相机升级或重连后 IP 改变：按实际 `cameraNamespace` 重新识别，只记录地址迁移，不按固定 IP 误报；
- 两个在线地址同时报告同一个 `cameraNamespace`：相机身份配置冲突；
- 相机可达但 Dashboard API 不通：Dashboard 后端退出或不可达；
- 帧率连续低于配置值的 80%：USB 带宽、CPU 或图像管线开始退化。

脚本不会重启服务、修改相机或停止录制。短时验证可加 `--duration 30`；查看关键事件可用：

```bash
rg '"event": "(fault_|usb_|camera_peer_)' outputs/camera_diagnostics/camera-monitor-*.jsonl
```

优先运行只读的深度诊断脚本。它会检查 NTP 实际选中源和偏差、CPU/内存/温度、
系统盘与录制盘、相机网卡、DDS/UDP 内核参数、RPS 与短时丢包增量、核心容器、
Dashboard/Preflight、三路相机输入帧率、建图定位、硬件图像管线、近期日志和语音服务。
异常项会同时给出原始证据、影响和按顺序执行的修复建议：

```bash
./scripts/system_doctor.sh
```

Shell 入口会自动显示完整证据，并把 JSON 保存为带时间戳的
`/tmp/insight_system_YYYYMMDD_HHMMSS.json`。默认全程只读；有故障时退出码为 `2`，
只有警告时默认仍为 `0`。自动巡检需要把警告也视为失败时，可运行
`./scripts/system_doctor.sh --fail-on-warning`。纯 JSON 输出用 `--json`，日志范围可用
`--log-since 2h` 调整。Shell 会优先自动使用 `~/.ssh/insight_camera_ed25519`；也可通过
`INSIGHT_CAMERA_SSH_IDENTITY` 指定其他 key。未找到 key 时，交互终端才会提示一次相机 SSH
密码。脚本在三台相机内部执行只读的 `ntpdate -q`，分别报告相机相对各自宿主机链路地址的
真实 NTP offset 和相机间最大差值。该检查与 `reboot_cameras.sh --sync-phase` 使用相同的
offset 查询，但不会执行后续的 `ntpdate -b` 时间同步、相机重启或相位调整，也不会把 HTTP
接口响应时机或 ROS 消息延迟误称为纯时钟差。私钥只保存在本机 `~/.ssh`，不得提交到仓库。

需要明确授权修复本轮检测到的问题时运行：

```bash
./scripts/system_doctor.sh --repair
```

修复模式先完成一轮只读诊断，并只对实际出现的异常调用已登记、可复检的修复动作：

- DDS/UDP 参数、RPS/RFS 异常：执行 `sudo -n ./deploy/host_setup.sh`；交互式 Shell 入口会先
  运行一次 `sudo -v`，避免每个主机修复步骤分别询问密码。
- 宿主机 NTP 异常：重启 chrony；核心容器缺失、1970 启动时间、Dashboard/媒体或建图服务
  运行态异常：用 Compose 启动或重建对应服务；语音服务异常：重启用户级语音服务。
- 相机时钟/采集相位：显式修复模式始终执行三台相机的 `ntpdate -b`、同步重启采集服务和
  10 秒图像时间戳测量，保留原有校准能力。

所有自动动作完成后脚本再次运行完整诊断；命令返回成功但对应检查仍异常时，修复项会改判为
故障，而不是只按命令退出码宣布成功。录制盘挂载、配置覆盖、数据删除、物理链路、资源压力、
场景纹理/定位和历史日志没有安全且无歧义的通用动作，报告会列入“需要人工处理”并保留原修复
建议。录制中会拒绝全部自动修复。相机校时后即使 NTP 恢复到 1 ms 内，只要图像相位仍超过
10 ms，最终结论仍为故障并返回退出码 `2`；采集服务恢复期间 Dashboard 看门狗可能重建后端
DDS participant，但不会重启整台相机。

脚本若在权限受限的容器中运行，会把无法读取的宿主机检查标为未验证；现场报障应直接
在 Jetson 宿主机仓库目录执行。修复建议可能包含重启或重挂载操作，录制中不得执行；
脚本检测到正在录制时会在报告结尾再次提示。

若问题与某次录制有关，同时附上该 bag 的检查结果：

```bash
docker exec insight-dashboard python3 scripts/check_bag.py rosbags/<目录名> > /tmp/insight_bag_check.txt
```
