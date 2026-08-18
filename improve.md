对，这个前提一改，我上一版里把“语音”和“前端”的定位判断反了。

按你现在这个真实产品形态：

> **数采员背着背包现场采集，全程无屏操作；语音是主交互；Web 前端是在采完以后用于查看、质检、处理、导出数据。**

我重新按这个逻辑审了当前 `main`。结论变成：

> **现在真正应该砍的不是语音，而是“为实时有屏 Dashboard 服务的一大批常驻能力”。**
>
> 同时，目前语音虽然已经做得比较深，但还没有真正成为整个产品的“一级控制系统”。

---

# 1. 我认为这个项目应该重新定义成 3 层

不要再把它理解成一个 Dashboard。

应该是：

```text
Insight Capture Backpack

① Field Capture Runtime
   数采员背着运行
   ↓
   语音
   相机
   VIO / Mapping
   Recording
   Live QC
   Storage
   Recovery

② Post-processing
   采完以后连接电脑
   ↓
   三路视频回放
   完整性
   VIO质量
   Hand pose
   Gripper
   LeRobot
   Dataset

③ Engineering / Maintenance
   开发/售后使用
   ↓
   CLI
   COLMAP
   RViz
   Dense Mapping
   OTA / Debug
```

目前仓库的问题就是：

**①②③ 混在同一个运行时、同一个 Docker image、同一个 Dashboard 进程里。**

---

# 2. 语音控制不仅不能删，反而应该成为 P0 核心

我重新看了你现在的语音代码。

其实你现在的方向已经比较对了。

固定命令：

```text
开始录制
停止录制
开始校准
检查相机
设置检测位
```

走的是：

```text
麦克风
 ↓
Silero VAD
 ↓
SenseVoice INT8
 ↓
确定性命令匹配
 ↓
localhost Dashboard API
 ↓
预生成语音反馈
```

**不经过 OpenClaw，也不经过大模型。**

例如代码已经明确：

```python
"recording_start": "/api/automation/recording/start"
"recording_stop": "/api/automation/recording/stop"
"calibration_start": "/api/mapping/reset"
"capture_check": "/api/capture-check/run"
```

这个思路非常适合背包。

### 所以这里我的新建议是：

**固定语音控制 = 核心产品**

**OpenClaw 自然语言 = Optional Assistant**

而不是把整个语音叫：

```text
OpenClaw Voice
```

---

# 3. 现在语音架构有一个很大的产品问题

虽然固定命令实际上**不需要 OpenClaw**，但是你的安装脚本却要求：

```bash
~/.openclaw/bin/openclaw
```

必须存在。

然后还会启动：

```text
openclaw-gateway.service
```

甚至 systemd 写的是：

```text
After=network-online.target ... openclaw-gateway.service
Wants=network-online.target openclaw-gateway.service
```

这对于一个**野外/现场无屏数采背包**来说不合理。

因为最重要的：

> “开始录制”

不能因为：

* 没网络
* OpenClaw 登录失效
* Codex 出问题
* Gateway 没起来

而影响整个声控服务。

---

# 4. 我会把它拆成这样

```text
looper-voice-control
│
├── VAD
├── SenseVoice
├── 本地命令
│   ├── 开始录制
│   ├── 停止录制
│   ├── 系统状态
│   ├── 开始校准
│   ├── 检查相机
│   └── 本条作废
│
├── 本地 TTS
│
└── optional
    └── OpenClaw adapter
```

系统服务应该变成：

```text
looper-voice-control.service
```

这个服务：

**完全离线也必须 100% 可用。**

然后：

```text
openclaw-gateway.service
```

挂了：

> 只影响“宸境，今天采了多少条？”这类自然语言。

**不能影响开始/停止采集。**

---

# 5. 现在仓库里真正很不合理的，是“无屏背包仍然一直跑显示链路”

这个是我重新审代码以后认为最应该优先改的地方。

你的 `multi_camera_dashboard_web.py` 启动时，无论有没有浏览器连接，都会：

```python
_create_pose_subscriptions()
_create_dashboard_image_subscriptions()
_create_hand_overlay_subscriptions()
```

而且：

```python
self._webrtc_proc = self._start_webrtc_worker()
```

WebRTC worker 也是**直接启动**的。

也就是说：

> 数采员背着包在现场，根本没人看网页，但实时 Dashboard 的媒体链路依然存在。

---

# 6. 更严重的是，现在没有浏览器时仍然会处理 Dashboard 图像

`ImagePipeline` 当前逻辑里：

```python
refresh_fallback = (
    topic_type == "compressed"
    or not self.owner._webrtc_has_sessions.get(camera_name)
    or ...
)
```

如果**没有 WebRTC viewer**：

```python
not self.owner._webrtc_has_sessions.get(camera_name)
```

就是 True。

随后：

```python
_encode_dashboard_frame(...)
```

仍然会被调用。

这意味着像两路 Insight3 raw infrared：

```text
ROS Image
 ↓
Dashboard subscription
 ↓
frame worker
 ↓
NVJPEG / JPEG encode
 ↓
latest_camera_frames
```

哪怕：

> **现场根本没有任何人打开 Dashboard。**

这对你的产品是一个非常典型的**多余运行时负担**。

---

# 7. 所以第一批应该优化的不是页面，而是 Headless Capture Mode

我会给系统明确增加：

```text
INSIGHT_MODE=capture
INSIGHT_MODE=review
INSIGHT_MODE=dev
```

### Capture 模式

背包现场默认：

```text
✓ ROS Recording
✓ Header audit
✓ Camera health
✓ VIO
✓ Insight9 mapping
✓ Insight3 localization
✓ Storage
✓ Voice
✓ Capture QC

✕ WebRTC
✕ JPEG preview encode
✕ Hand overlay
✕ Playback subscriptions
✕ Babylon.js live rendering supporting worker
✕ COLMAP
✕ WiLoR inference
```

### Review 模式

采完以后：

```text
✓ Web UI
✓ Bag playback
✓ Video preview
✓ Hand pose
✓ Dataset
✓ Scoring
✓ WiLoR
```

### Dev 模式

```text
✓ RViz
✓ Dense Mapping
✓ COLMAP
✓ Debug
✓ Validation
```

这个改动比“删几个 Python 文件”重要得多。

---

# 8. 但这里不能简单把 Dashboard 图像订阅全删掉

代码里有一个比较重要的耦合。

现在 Insight3 global localization 用的 2 Hz 图像，是复用 Dashboard 图像 reader 的：

```python
_maybe_relay_localization_image()
```

并发布：

```text
/insight_mapping/insight3_a/infra1/image_rect_raw
/insight_mapping/insight3_b/infra1/image_rect_raw
```

所以不能直接：

> 没屏幕 → `_create_dashboard_image_subscriptions()` 删除。

应该把它拆成：

```text
Camera Capture Reader
│
├── recording header audit
├── 2Hz localization relay
│
└── Preview Pipeline   ← 只有浏览器需要时才启动
      ├── JPEG
      ├── WebRTC
      └── overlay
```

这样更合理。

---

# 9. WebRTC 不应该删除，但应该 Lazy Start

`WorkerSupervisor` 其实已经会判断：

```python
if not self.owner._webrtc_has_sessions.get(camera_name):
    return
```

不会向 WebRTC worker 发帧。

但是：

**worker 进程本身仍然开机就 spawn。**

这没必要。

应该变成：

```text
浏览器第一次请求 Live Preview
           ↓
start webrtc_worker
           ↓
有人观看
           ↓
encode / IPC
           ↓
最后一个 viewer 离开
           ↓
30~60 秒后 worker shutdown
```

这样才符合背包架构。

---

、
# 11. `/dev/snd` 现在挂错地方了

这也是一个比较明确的冗余。

Compose 现在给：

```text
insight-dashboard
```

挂：

```yaml
devices:
  - /dev/snd:/dev/snd
```

注释说是给 voice worker。

但你现在的语音实际上是：

> **宿主机 systemd service**

而不是 Dashboard container。

文档也明确说：

> Dashboard 不再包含另一套麦克风 worker。

所以：

```text
/dev/snd → insight-dashboard
```

可以删。

音响/麦克风权限应该只属于：

```text
looper-voice-control.service
```

权限边界也更干净。

---

# 12. 第二批我会删/移出的功能：Gesture Recording

在“无屏 → 语音主控”这个设定下：

```text
双手点赞开始/停止录制
```

反而成了重复输入方式。

你现在已经有：

```text
Voice
Web
Gesture
```

三个地方可以修改 Recording state。

这会增加状态机复杂度。

所以除非你明确希望：

> **环境特别吵的时候，手势作为声控备用方案**

否则我会把它删掉。

包括：

```text
dashboard_runtime/gesture_recording.py
hand_tracking/gestures.py
hand_gestures.py
相关 Settings
相关 tests
```

现在配置默认本来也是：

```json
"gesture_recording": {
    "enabled": false
}
```

说明它本身就不是当前默认主流程。

---

# 13. Avatar / Iron Man 这一套也可以砍

现在的配置：

```text
Insight3 → vis_assembly.glb
Insight9 → iron-man_helmet_mk3_optimized.glb
```

对于一个：

> **采后数据 QC 工具**

我反而建议 3D 里展示：

```text
Head frame
Left camera frame
Right camera frame
TCP
trajectory
axes
uncertainty
tracking quality
```

而不是：

```text
Iron Man helmet
glove
avatar
```

这些资产：

```text
ArmBaseModel_BravFG.glb
MaleBaseModel_BravFG.glb
glove.glb
iron-man_helmet_mk3_optimized.glb
vis_assembly.glb
```

都可以逐步取消。

简单 axis / frustum 对质检其实更专业、更准确。

---

# 14. Stick Figure 我也倾向于删

原因不是不好看，而是：

**它可能误导用户。**

你自己的使用说明已经注明：

> 手臂是固定臂长 IK 合成，是合理近似，不是真实追踪。

如果前端定位是：

> **数据后处理 + 数据质量判断**

那一个“看起来像真实人体姿态、实际上是合成”的东西价值不高。

保留真正的：

```text
WiLoR hand pose
21 keypoints
MANO
camera/global pose
```

更好。

---

# 15. Handpose 功能保留，但独立 `/handpose` 页面可以降级

这一点也和上一版不同。

WiLoR 对 Ego 数据是有价值的，所以：

```text
scripts/handpose/
scripts/ego_lerobot/
WiLoR
```

**不要删。**

但普通用户实际上在：

```text
Dataset Export
```

时，系统已经可以自动识别：

```text
UMI gripper route
vs
Ego hand route
```

Ego route 会自动跑 WiLoR。

所以：

```text
/handpose
```

更像：

> 算法工程师检查 WiLoR 的调试页。

建议放：

```text
Advanced / Diagnostics
```

而不是主要后处理导航。

---

# 16. COLMAP 更应该移出主产品

这次我会比之前更坚定。

因为文档写的是：

> 1–2 分钟录制，COLMAP 约 10–15 分钟，而且运行时建议不要录制。

这和批量数采的核心目标：

> episode / hour

是冲突的。

所以：

```text
/optimization
COLMAP
looper-vio-colmap-handoff
```

应该属于：

```text
Engineering diagnostics
```

不是日常后处理。

---

# 17. 更重要的是，COLMAP 和 WiLoR 现在都被塞进了现场 Runtime 镜像

当前 Dockerfile 的 `runtime` 直接包含：

```text
COLMAP
CUDA runtime
looper-vio-colmap-handoff
PyTorch
WiLoR
MANO
Ultralytics
SciPy
Firefox
GStreamer/WebRTC
```

而 `build_release.sh` 还写得很直接：

> Dashboard image tarball several GB。

这对于一个背包现场 Runtime 太重了。

---

# 18. 我强烈建议拆成两个 Image

## `insight-capture-runtime`

永远运行：

```text
ROS
MCAP recorder
camera health
header audit
mapping
relocalization
voice API
storage
recovery
```

## `insight-postprocess`

采完以后才启：

```text
Web UI
Playback
WiLoR
Handpose
LeRobot
UMI
Scoring
COLMAP（甚至再单独 diagnostics）
```

这样即使两套都存在 Jetson：

```text
采集中：
capture-runtime      RUNNING
postprocess           STOPPED
```

采完：

```text
capture-runtime      RUNNING / IDLE
postprocess           RUNNING
```

这样资源和产品边界会清楚很多。

---

# 19. SuperGlue / Sparse Mapping 不能删

这一点特别要区分。

虽然文件叫：

```text
Dockerfile.superglue-validation
```

但现在它已经是：

```text
Insight9 sparse mapping
+
Insight3 global localization
```

的正式运行依赖。

所以：

```text
insight9_sparse_mapper.py
insight3_global_localizer.py
insight9_mapping_core/
SuperGlue
```

这些是背包的**核心运行时**。

应该保留。

但建议改名：

```text
Dockerfile.superglue-runtime
```

别再叫 `validation`。

---

# 20. Dense Mapping / RViz 可以明确移出

这些现在本来也只在：

```text
mapping-validation
```

profile 里：

```text
Dockerfile.rviz-validation
config/rviz/
scripts/run_mapping_validation_rviz.sh
scripts/insight9_dense_mapper.py
insight9_mapping_core/dense_stereo.py
```

建议统一移动：

```text
tools/mapping_validation/
```

不要和正式 backpack runtime 混在一起。

---

# 21. `inprocess_bag_writer.py` 现在高度疑似已经可以删

这个是重新审的时候又发现的一处技术债。

`inprocess_bag_writer.py` 还是老的：

```text
Dashboard image callback
→ serialize_message
→ rosbag2_py
→ SQLite writer
```

但你现在新的 `RecordingBridge` 已经明确写：

> **native rosbag2 process owns all storage writes**。

Dashboard image callback 只负责：

> live image-header audit。

所以：

```text
scripts/inprocess_bag_writer.py
```

已经是很强的**删除候选**。

不过我还是建议删除前在本地执行一次：

```bash
rg "InProcessBagWriter|inprocess_bag_writer" .
```

如果没有调用，直接删。

现在 `scripts/README.md` 还把它写成“关键稳定入口”，这个文档也已经落后于代码。

---

# 22. SQLite / composite 那批不要立刻删

这一批：

```text
post_processing_core/sqlite_merge.py
post_processing_core/composite_bag.py
config/rosbag_storage_sqlite3.yaml
```

新的采集已经是单 native MCAP。

但是代码明显仍然在兼容历史 SQLite/composite bag。

所以应该：

```text
legacy/
```

而不是现在就删。

等确认：

> 老 rosbag 已经不再需要打开。

再一次性删除整个 legacy compatibility。

---

# 23. `looper_cli` 我现在不建议删了

上一版我倾向于拆出去。

考虑到“背包设备”以后，我调整这个判断：

它对于：

```text
装机
售后
OTA
网络
DDS
设备恢复
日志
相机 FPS
```

其实非常有用。

所以可以留。

但它是：

> **维护人员工具**

不是：

> 数采员功能。

更合适：

```text
tools/device_cli/
```

---

# 24. 反而现在发布流程的优先级很奇怪

`build_release.sh` 当前会把：

```text
looper_cli
```

塞进客户部署包。

但是没有打包：

```text
openclaw_voice_bridge.py
run_openclaw_voice.sh
install_openclaw_voice_service.sh
openclaw-voice.service.in
语音模型安装
```

也就是说：

> **维护 CLI 是一等公民，数采员唯一现场交互——语音——反而不是正式 Release 的一部分。**

这对于你的产品定义来说是现在最大的结构问题之一。

---

# 25. 所以 P0 第一件事情就是：Voice First-class Deployment

我会要求新的新机部署流程是：

```text
安装完成
 ↓
自动识别麦克风/音响
 ↓
加载 SenseVoice
 ↓
加载 VAD
 ↓
加载 Piper
 ↓
启动 looper-voice-control
 ↓
检查 Dashboard API
 ↓
播放：

“数采系统已就绪”
```

而不是现在还要另外手动准备：

```text
~/.local/share/looper-voice/
```

然后再运行：

```bash
install_openclaw_voice_service.sh
```

---

# 26. ALSA 设备现在也不够产品化

目前默认硬编码：

```text
plughw:E3,0
```

对于背包来说很危险。

USB 声卡：

* 重插；
* 开机枚举顺序变化；
* 更换同型号设备；

都有可能导致 card index 改变。

应该按：

```text
USB VID/PID
设备名
udev stable identifier
```

发现设备。

例如内部配置最终应该是：

```text
input_device: auto
output_device: auto
preferred_device: E3
```

而不是把 `plughw:E3,0` 写死在代码默认值里。

---

# 27. 真正从“数采员”出发，我认为现在最缺的是这几个语音能力

### 第一：`系统状态`

这是无屏系统最重要的命令之一。

数采员说：

> **“系统状态”**

返回应该类似：

> 三台相机正常，定位正常，时间同步正常，剩余空间 312 GB，可以开始采集。

异常时：

> 右手相机未定位，暂时不能开始采集。

不是让数采员猜。

---

# 28. 第二：开始录制之前必须自动 Preflight

现在的：

> “开始录制”

不应该简单等价于：

```text
POST /recording/start
```

而应该：

```text
开始录制
 ↓
Camera online?
 ↓
FPS?
 ↓
Mapping ready?
 ↓
Insight3 localized?
 ↓
Storage mounted?
 ↓
Storage writable?
 ↓
Disk enough?
 ↓
Time sync?
 ↓
Recorder topics ready?
 ↓
START
```

通过：

> **“录制已经开始。”**

失败：

> **“无法开始录制，右手相机未定位。”**

或者：

> **“无法开始录制，存储盘不可用。”**

这比任何 Dashboard UI 都重要。

---

# 29. CaptureCheck 要和 Preflight 分开

你现在的 `CaptureCheckManager` 本质其实不是“系统健康检查”。

它是：

> **固定检测位重复性检查。**

它要求：

```python
REQUIRED_ROLES = ("head", "left_hand", "right_hand")
```

并把当前三个 global Pose 和保存的基准比较。

这对于有固定治具的批量采集很有价值。

但它不应该叫：

```text
capture_check
```

更准确：

```text
station_check
```

然后：

```text
system_preflight
```

是另一套东西。

---

# 30. 特别是背包场景，不应该默认每条都要求固定 Pose

现在停止录制后的固定播报是：

> “请将三台相机放回检测位，静止后说检查相机。”

如果你的某一种 SOP 本身就有固定检测治具，这很好。

但是不要硬编码成整个 backpack 产品的规则。

应该在 Task Profile 里：

```yaml
cup_stacking:
  station_check_after_take: true

free_motion:
  station_check_after_take: false
```

否则以后做不同采集任务会很难扩展。

---

# 31. 第三：录制过程中异常必须主动“说出来”

因为没有屏幕。

现在如果采集中：

```text
Insight3 B VIO drift
camera stale
USB 掉线
磁盘快满
写盘 fallback
localization lost
```

不能等数采员采完回去看网页。

应该：

```text
轻微异常
→ 不打扰

持续异常 > N 秒
→ 音响提醒

严重异常
→ 本条自动标记 suspect / invalid
```

例如：

> **“右手相机定位丢失。”**

然后继续录原始数据，但本条：

```json
{
  "quality": "invalid",
  "reason": "right_camera_localization_lost"
}
```

---

# 32. 不建议遇到问题就直接停止录制

这里特别重要。

现场一旦出现问题：

> 不要自动删除。

也不要轻易中断原始数据。

应该：

```text
继续保存 raw bag
+
记录 anomaly timeline
+
把 take 标成 suspect/invalid
```

后面还能排查。

---

# 33. 第四：需要“本条作废”

数采员自己最清楚：

* 手碰错了；
* 任务做失败；
* 物体掉了；
* 中途有人干扰；
* 操作流程错了。

所以非常需要：

> **“本条作废。”**

不是删除 bag。

而是：

```json
{
  "take_id": 37,
  "operator_valid": false,
  "invalid_reason": "operator_rejected"
}
```

然后语音：

> **“第 37 条已标记作废。”**

这个对训练数据管理特别重要。

---

# 34. 第五：Session / Take 是现在最缺的数据模型

现在核心还是：

```text
looper_record_20260818_103050
```

但数采员脑子里不是 filename。

而是：

```text
今天
任务：叠杯
目标：100 条

第 1 条
第 2 条
...
第 37 条
```

所以应该正式引入：

```text
Session
 ├── Task
 ├── Operator
 ├── Environment
 └── Takes
       ├── 001
       ├── 002
       └── ...
```

每条 Take 关联：

```text
bag
start/end
voice trigger
live QC
station check
operator accept/reject
postprocess QC
dataset export
```

这是我认为产品现在**最大的功能缺口**。

---

# 35. 最理想的现场语音流程应该极其简单

上电：

> **“系统启动中。”**

一切 Ready：

> **“数采系统已就绪。”**

数采员：

> “开始录制。”

系统：

> “初始化录制中，请稍等。”

然后：

> “第十二条录制已经开始。”

完成：

> “停止录制。”

系统：

> “正在结束录制。”

快速检查完成：

> “第十二条录制完成，数据正常。”

或者：

> “第十二条录制完成，右手相机存在异常，已标记复检。”

操作失败：

> “本条作废。”

系统：

> “第十二条已标记作废。”

然后下一条。

**这才是背包产品的 UI。**

音响就是 UI。

---

# 36. 这里也会改变“自动 Scoring”的策略

上一版我说：

> Stop 后立刻自动 Scoring。

放到背包场景里，我会修正：

### Stop 后立即做

只做轻量检查：

```text
live header audit
message count
camera continuity
recording exit
disk write
mapping/localization history
network audit
```

给数采员几秒内答案：

```text
PASS / SUSPECT / FAIL
```

### 不要每条都立即做

```text
COLMAP
WiLoR
完整视频编码
深度 trajectory scoring
dataset export
```

因为这些会抢 Jetson CPU/GPU/IO。

---

# 37. 重后处理应该排队

例如：

```text
Take 001   quick PASS
Take 002   quick PASS
Take 003   quick SUSPECT
...
```

数采员说：

> “结束任务。”

或者系统检测：

```text
连续 5 分钟没有录制
```

再进入：

```text
Postprocess Queue
```

或者等接回工作站再处理。

**采集吞吐永远优先。**

---

# 38. 采后 Web 前端应该怎么改

既然你明确说：

> **Web 是后处理。**

那首页就不应该是 `/3d`。

应该直接是：

```text
Sessions
```

例如：

```text
2026-08-18 · Cup stacking

Progress
86 / 100

Accepted      79
Suspect        5
Rejected       2

Postprocess
██████████░░ 72%
```

点进去：

```text
Take 037

[三路同步视频]

Timeline
────────────────────────
camera          ✓
frame loss      ✓
VIO             ⚠
localization    ⚠
operator        ✓
station check   ✓

Reason
right-hand localization lost
12.4s → 14.1s
```

这比 8 个孤立页面实用很多。

---

# 39. 页面我会重新收敛成

### 普通后处理用户

```text
Sessions
Review
Datasets
Storage
```

### Advanced

```text
Hand Pose
Trajectory
Mapping
Calibration
```

### Engineering

```text
COLMAP
RViz
Dense Mapping
System Diagnostics
```

所以不是：

> 前端没用。

而是：

> **前端的 IA（信息架构）应该从“算法功能菜单”变成“数据处理工作流”。**

---

# 40. 最后给你一个重新审完后的“删 / 留 / 改”表

| 功能/文件                      | 新判断                    |
| -------------------------- | ---------------------- |
| 本地固定语音控制                   | **绝对保留，P0 核心**         |
| SenseVoice / VAD / Piper   | **保留**                 |
| OpenClaw 自然语言              | 保留为 Optional           |
| OpenClaw 与固定控制强耦合          | **必须拆**                |
| `/dev/snd` 挂进 Dashboard    | **删除**                 |
| `open_web_3d_right.sh`     | **删除/Dev only**        |
| X11 / DISPLAY              | **删除正式 Runtime**       |
| WebRTC                     | 保留，但 **lazy start**    |
| 无 viewer 时 JPEG preview 编码 | **必须修**                |
| Live 3D                    | 后处理/Debug              |
| Avatar / Iron Man          | **建议删除**               |
| Stick Figure               | **建议删除**               |
| Gesture Recording          | **建议删除或备用模式**          |
| Sparse Mapping             | **核心，保留**              |
| Insight3 Global Localizer  | **核心，保留**              |
| SuperGlue                  | **核心，保留并改 runtime 命名** |
| Dense Mapper               | Engineering            |
| RViz                       | Engineering            |
| WiLoR                      | Postprocess，保留         |
| `/handpose` 独立页            | Advanced               |
| COLMAP                     | Engineering            |
| Legacy UMI                 | Advanced/Legacy        |
| LeRobot                    | **核心后处理**              |
| Scoring                    | 核心能力，但并入 Take Review   |
| `inprocess_bag_writer.py`  | **强删除候选**              |
| SQLite/composite           | Legacy，暂留              |
| `looper_cli`               | Maintenance，保留         |
| Voice 不进入 release bundle   | **当前严重缺口**             |

---

## 我现在认为最值得立刻做的 5 件事

**第一，做真正的 `Capture Mode`。** 无 WebRTC viewer 时停止 JPEG preview、overlay、playback 等所有显示负担。

**第二，把 Voice 从 OpenClaw 中剥离。** `looper-voice-control` 是离线核心，OpenClaw 只是插件。

**第三，让 Voice 正式进入 `build_release` 和首次部署。** 现在 release 带 CLI 不带语音，这和产品定义完全倒挂。

**第四，做 Session / Take + 语音状态机。** 不再把 rosbag 文件名当作产品数据模型。

**第五，做实时 QC → 语音主动告警。** 无屏数采系统里，**“系统自己发现问题并告诉数采员”比任何 Dashboard 页面都重要。**

这样改完以后，这个项目就不再是 `insight-capture-dashboard` 的思路，而是真正变成一个 **Insight Data Acquisition Backpack System**。



代码结构期望为
insight-capture-dashboard/
│
├── README.md
├── Dockerfile
├── docker-compose.yml
├── .gitignore
├── .dockerignore
│
├── config/
│   ├── devices/
│   │   ├── jetson-nx/
│   │   │   ├── cameras.json
│   │   │   └── runtime.json
│   │   └── ...
│   │
│   ├── capture_profiles/
│   │   ├── dual_arm_umi.json
│   │   ├── ego_hands.json
│   │   └── vio_benchmark.json
│   │
│   ├── gripper_calibration.json
│   └── rosbag_qos_overrides.yaml
│
├── insight_capture/
│   │
│   ├── runtime/                         # ★ 背包现场核心
│   │   ├── app.py
│   │   ├── camera_health.py
│   │   ├── preflight.py
│   │   ├── session.py
│   │   ├── take.py
│   │   ├── anomaly.py
│   │   ├── watchdog.py
│   │   │
│   │   ├── recording/
│   │   │   ├── manager.py
│   │   │   ├── recorder.py
│   │   │   ├── storage.py
│   │   │   ├── header_audit.py
│   │   │   └── recovery.py
│   │   │
│   │   └── mapping/
│   │       ├── insight9_mapper.py
│   │       ├── insight3_localizer.py
│   │       ├── pose_graph.py
│   │       ├── relocalization.py
│   │       └── superglue_backend.py
│   │
│   ├── voice/                           # ★ 数采员真正的 UI
│   │   ├── service.py
│   │   ├── commands.py
│   │   ├── audio.py
│   │   ├── stt.py
│   │   ├── tts.py
│   │   ├── replies.py
│   │   └── openclaw_adapter.py          # optional，不影响固定指令
│   │
│   ├── postprocess/                     # ★ 采后重处理
│   │   ├── bags/
│   │   │   ├── catalog.py
│   │   │   ├── integrity.py
│   │   │   ├── playback.py
│   │   │   └── synchronization.py
│   │   │
│   │   ├── quality/
│   │   │   ├── trajectory_score.py
│   │   │   └── station_check.py
│   │   │
│   │   ├── handpose/
│   │   │   ├── wilor.py
│   │   │   ├── filtering.py
│   │   │   └── schema.py
│   │   │
│   │   ├── gripper/
│   │   │   ├── tracking.py
│   │   │   ├── calibration.py
│   │   │   └── extraction.py
│   │   │
│   │   ├── datasets/
│   │   │   ├── routing.py
│   │   │   ├── lerobot.py
│   │   │   ├── ego_lerobot/
│   │   │   └── umi_legacy.py
│   │   │
│   │   └── optimization/
│   │       └── colmap.py
│   │
│   ├── media/                           # ★ 只有需要显示时才工作
│   │   ├── image_pipeline.py
│   │   ├── jpeg.py
│   │   ├── webrtc.py
│   │   ├── webrtc_worker.py
│   │   └── preview_manager.py
│   │
│   ├── web/                             # Web 后端
│   │   ├── app.py
│   │   ├── context.py
│   │   ├── websocket.py
│   │   └── routes/
│   │       ├── sessions.py
│   │       ├── takes.py
│   │       ├── recording.py
│   │       ├── cameras.py
│   │       ├── playback.py
│   │       ├── datasets.py
│   │       ├── handpose.py
│   │       ├── quality.py
│   │       └── settings.py
│   │
│   ├── common/
│   │   ├── models.py
│   │   ├── paths.py
│   │   └── config.py
│   │
│   └── legacy/                          # 有明确退出期限
│       ├── sqlite_bag.py
│       ├── composite_bag.py
│       └── umi_zarr.py
│
├── web_dashboard/                       # 浏览器前端
│   ├── src/
│   │   ├── sessions/
│   │   ├── review/
│   │   ├── datasets/
│   │   ├── storage/
│   │   ├── advanced/
│   │   │   ├── handpose/
│   │   │   ├── trajectory/
│   │   │   └── optimization/
│   │   └── shared/
│   ├── dist/
│   ├── build.js
│   └── package.json
│
├── scripts/                              # ★ 只允许“薄入口”
│   ├── run_dashboard.sh
│   ├── run_voice.sh
│   ├── select_device.sh
│   ├── reboot_cameras.sh
│   ├── sync_camera_restart.py
│   ├── check_bag.py
│   └── export_lerobot.py
│
├── tools/                                # ★ 数采员完全不用碰
│   ├── device_cli/
│   ├── mapping_validation/
│   │   ├── rviz/
│   │   ├── dense_mapper.py
│   │   └── run_validation.sh
│   └── diagnostics/
│
├── deploy/
│   ├── docker-compose.yml
│   ├── update.sh
│   ├── README.md
│   └── systemd/
│       ├── insight-capture.service
│       ├── insight-voice-control.service
│       ├── insight-camera-network.service
│       └── insight-camera-reboot.service
│
├── docs/
│   ├── USAGE.md
│   ├── DEPLOYMENT.md
│   ├── DATA_FORMAT.md
│   ├── VOICE_CONTROL.md
│   ├── CAPTURE_SOP.md
│   └── ARCHITECTURE.md
│
└── tests/
    ├── runtime/
    ├── voice/
    ├── postprocess/
    ├── web/
    └── legacy/