# Insight9 配置与维护说明

适用：Insight9 头部 + Insight3 A 右腕 + Insight3 B 左腕，设备配置 `jetson-nx`。供安装、维护和数据交接人员使用，日常操作分别见《手机采集指南》和《电脑 Qt 使用指南》。

核对日期：2026-11-18。对应源码与远端 `main` 一致：`5bd32c0bfa6d7b84d71d4b657e9996aa54f0c7e6`。源码声明版本为 **2.1.0**，现场安装版本读取安装目录 `.env` 中的 `INSIGHT_VERSION`，并核对实际运行产物。不能把源码声明版本直接当作现场版本。

## 1 项目由哪些部分组成

手机和 Qt 是同一后端的两个操作端，不是两套独立采集系统。

| 部分 | 技术与职责 |
| --- | --- |
| 手机端 | Vue 页面、WebRTC 预览；连接 Jetson 热点，完成任务选择、起停、记录查询和设备诊断 |
| Qt 桌面端 | C++、QML、Quick 3D、GStreamer；在 Jetson 宿主机运行，提供空间显示、录制管理与后处理 |
| 共用后端 | Python aiohttp、HTTP/WebSocket、ROS 2；管理任务、Take、输入、时钟、录制和后处理 |
| 建图定位 | LooperVIO C++、特征推理与 ROS 适配；头部建图，双腕定位到公共地图 |
| 数据与后处理 | Jetson 本地 MCAP、回放缓存、完整性、VIO 轨迹评分、COLMAP 重建 |

后端和算法服务由 Docker 等部署服务管理。依赖已安装的设备可离线使用，但仍需热点、相机 USB 链路及本机后端工作。

手机连接方式为：**手机 → Jetson Wi-Fi 热点 → 采集服务**。三台相机各自通过 USB 接入 Jetson。手机不直接连接相机。入口为 `http://10.77.0.1`，备用为 `http://10.77.0.1:8765/mobile`；热点名称和密码按现场配置。

旧桌面网页 `/3d`、`/bags`、`/scoring` 不再作为桌面操作入口，使用 Qt。旧 UMI/LeRobot 导出、手部骨架提取和夹爪开合追踪已移除；任务元数据中的 `dual_arm_umi` 名称不代表这些功能仍可用。

## 2 分支 软件版本和设备配置怎么区分

**本套设备只使用一个设备配置：`jetson-nx`。** Git 分支是代码历史，软件版本是发布身份，配置是硬件和运行参数，现场标定文件是设备状态，不能互相替代。

2026-11-18 本地 Git 快照：**1 个本地分支 `main`，3 个远端跟踪分支**。两者包含同一条主线，不能相加理解成四套软件。

| 分支 | 当前含义 |
| --- | --- |
| `main` / `origin/main` | 当前主线及本说明的源码基线 |
| `origin/codex/stream-startup-recovery` | 已包含于主线的历史开发分支 |
| `origin/hz-capture-v1-aruco` | 仍有独有提交的历史分叉，不是本套设备的配置入口 |

另有 9 个标签、1 个 detached worktree；这些均不增加硬件配置数量。`origin/HEAD` 是默认分支指针。当前没有 `v2.1.0` 标签；源码声明版本不能单独证明现场安装身份。后续交接需重新查询 Git。

### 配置文件分为四类职责

| 类别 | 位置 | 含义 |
| --- | --- | --- |
| 设备模板 | `config/devices/jetson-nx/cameras.json`、`runtime.json` | 本套设备的相机、预览、时钟、录制、预检和 QC 默认值 |
| 实例配置 | `config/cameras.json`、`config/runtime.json`、`config/.device` | 当前安装使用的配置和上次选择名；现场可能有修改 |
| 部署设置 | `.env` | 主机存储挂载等部署参数 |
| 任务和现场状态 | `config/capture_tasks.json`、QoS、相机标定与对齐文件 | 任务元数据、录制策略及现场校准结果 |

这是四类文件职责，不是四个设备配置。`.device` 写着 `jetson-nx` 也不能证明生成文件与模板完全相同，交接要核对实际内容。

### 三台相机的固定角色

| 设备 | namespace | 角色 |
| --- | --- | --- |
| Insight9 头部 | `insight9_a` | `head` |
| Insight3 A 右腕 | `insight3_a` | `right_hand` |
| Insight3 B 左腕 | `insight3_b` | `left_hand` |

角色按 namespace 识别，不按插口、接入顺序、画面排列或 IP 分配。USB CDC-NCM 地址动态发现，无固定相机 IP 白名单。本配置主机 ROS domain 为 20；三条 USB 网段应互不冲突，也不与热点或调试网冲突。首次接入或换机由维护人员准备相机 namespace、ROS domain、时钟适配和 DDS 直连。

## 3 当前启动方式和配置操作

本文“项目或安装根目录”指 Jetson 上包含 `docker-compose.yml`、`config/`、`scripts/` 和 `native_dashboard/` 的目录。2026-11-18 已通过 SSH 核实 `nvidia@192.168.19.222` 使用 `/home/nvidia/insight-capture-dashboard`，现场源码为 `011a065`，未更新到本文的主线基线。另一独立服务 `/home/nvidia/jetson-capture` 也在运行，不能作为旧项目副本删除。其他设备仍按其实际安装位置核对。

### 开机后先等待就绪

jetson-nx 标准配置使用四个后端服务：

| 服务 | 职责 |
| --- | --- |
| `insight-dashboard` | 手机与 Qt 共用接口、设备状态、任务和录制管理 |
| `superglue-inference` | 建图与定位所需的特征推理 |
| `insight9-sparse-mapper` | Insight9 头部稀疏建图 |
| `insight3-global-localizer` | 双腕定位到公共地图并提供相应位姿 |

四个容器均配置 `restart: unless-stopped`。已经安装并启动过，且 Docker 开机运行、容器未被手动停止时，容器会随 Docker 恢复。日常先等待服务和设备就绪，不必每次手动运行后端启动脚本。手动 stop、down、容器被删除或首次未完成安装时，不能依靠这个策略恢复。

标准 jetson-nx 模板不需要跨 ROS 域转发、额外校正和 H.264 编码服务；启动脚本按实际实例配置选择服务，因此现场自定义配置须另核对。

仓库的 `deploy/systemd/insight-capture.service` 是可部署的启动模板，不能证明现场已启用。现场自启应分别核对 Docker 是否 enabled、容器实际重启策略，以及是否另装了该 systemd 单元。Qt 是独立桌面进程，后端自启不代表 Qt 自动打开。

### 日常使用

手机连接 Jetson 热点后打开 `http://10.77.0.1`，不需要手机端启动命令。Qt 在 Jetson 的项目或安装根目录执行：

```bash
./native_dashboard/run.sh
```

它只启动 Qt，不负责启动后端。默认连接 `http://127.0.0.1:8765`，以 30 FPS 全屏运行。客户机直接使用已安装的发布程序和运行库，不在日常启动时编译。

### 先确认服务是否可响应

以下在 Jetson 项目或安装目录执行：

```bash
curl -fsS http://127.0.0.1:8765/healthz
```

返回 `{"ok": true}` 只说明 HTTP 后端可响应。仍须查看三台设备的图像、VIO、时间与存储状态。

### 维护人员需要启动后端时

先核对没有正在录制、保存或后处理的任务。启动脚本在空闲且服务已运行时可能重启后端，因此不是查询命令。

```bash
./scripts/run_dashboard.sh
```

这个入口启动服务、等待健康后返回，不打开浏览器。`scripts/run_dashboard.sh` 转交 `deploy/run_dashboard.sh` 执行；当前发布安装器会把这两个脚本同步到安装目录。

只查看状态和最近日志时，使用：

```bash
docker compose ps
docker compose logs --tail 100 insight-dashboard
```

在实际 Jetson 上核对自启状态，可只读执行：

```bash
systemctl is-enabled docker.service
docker inspect --format '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' insight-dashboard superglue-inference insight9-sparse-mapper insight3-global-localizer
systemctl is-enabled insight-capture.service
```

最后一项可能报告单元不存在；Docker 容器重启策略仍可能提供自启。是否安装和启用哪个单元，以现场输出为准，不根据源码模板推断。

Qt 需要匹配的 Jetson/NVIDIA 媒体栈和运行库，普通 PC 不能默认替代。显示目标帧率不等于传感器频率。确需自定义 Qt 参数时查阅 `native_dashboard/run.sh` 与原生参数定义；显式传参后脚本不再补齐默认全屏和帧率。

### 重新应用 jetson-nx 配置

只有需要重置实例配置时才执行。先停采集、等待保存、备份现场配置并核对差异：

```bash
./scripts/select_device.sh jetson-nx
```

该命令复制模板、覆盖实例配置并重新加载服务。`--no-start` 仍覆盖文件，只是不操作 Docker，适用于离线准备。执行后重新检查输入、时间和校准，不把它当作查看配置的命令。

本次文档工作未变更现场配置。写作所用开发 checkout 中 `.device` 为 jetson-nx，但生成 cameras 与模板不一致，且生成 runtime 缺失；部署时应单独核对，不能用该开发状态推断设备状态。

## 4 默认门槛和定位含义

| 项目 | jetson-nx 模板值或规则 |
| --- | --- |
| 时钟检查 | 开启，NTP 采样 3 次；单设备时钟上界 10 ms、两设备合计上界 20 ms、NTP RTT 上限 10 ms |
| 相机发现和校时 | USB 动态发现，持续相机校时开启 |
| 起录输入新鲜度 | 图像、位姿默认均为 2 s |
| 起录可用空间 | 默认至少 5 GiB，仍需预估整条录制容量 |
| 录制中 QC | 持续窗口 3 s，图像陈旧阈值 2 s，低空间告警 2 GiB |
| 原始数据和结果 | 默认逻辑目录 `rosbags`、`outputs/results` |
| 录制缓存 | 1 GiB，不能当作可用盘容量 |
| 主存储要求 | `require_primary_storage=false`，备用存储告警不一定阻止起录 |
| 地图与定位 | 模板严格预检要求二者；手机和 Qt 常用录制入口允许尚未全局定位的原始采集 |
| 腕部定位遮罩 | `localization.gripper_mask_height_ratio=0` |

放宽地图定位要求，不会免除图像、VIO、时钟、话题和存储预检。直接使用低层录制 API 时，应核对其预检模式，不能照搬界面行为。

默认头部预览为 `color_compressed`，腕部为 `infra1`；头部建图使用校正双目。预览画面不代表全部录制话题。默认话题方案包含公共位姿、本地 VIO、IMU、所配置图像流及相机参数、适用协方差话题、额外配置话题和 `/tf_static`，实际以 metadata 和录制清单为准。

公共坐标系为 `insight9_map`，统一位姿以双目中心表示，目标发布 30 Hz。在线校准重建地图与定位关系，不修改相机内参。标定板用于独立精度评估，不作为自然场景地图约束。

NTP 通过不能证明硬件曝光同步；绝对日期、UTC/RTC 和相机相对时钟也应分别核对。图像时间基准检查用于发现错误日期、旧偏移等问题，不能当作毫秒级同步精度证据。

专用 `vio_calibration` 采集两台 Insight3 固定的 17 个双目、VIO、IMU、参数和静态 TF 话题，不登记普通 Take，由维护人员使用。它与页面“重新校准”是两个不同流程。

## 5 存储 备份和异常收尾

所有录制保存到 Jetson 本地。实际采集盘及路径以界面输出为准，任务子目录由系统生成，不按显示名称猜测。

```text
<录制根目录>/<任务存储子目录>/<bag_name>/
  metadata.yaml
  <数据文件>.mcap
  recording_manifest.json
  image_header_audit.json       # 有相关记录时
  recording_network_audit.json  # 有相关记录时
  review/                      # 手动准备回放后生成
```

当前正式路径使用单个 rosbag2 目录、单个 MCAP 文件，内部可有多个 Chunk。保留整个目录及 metadata，旧 SQLite、旧组合包和多 MCAP 分包不属于当前读取路径。

| 内容 | 位置与处理要求 |
| --- | --- |
| 原始暂存 | `rosbags/_staging`，进行中或保存失败的数据，不能作为缓存清空 |
| 任务与 Take | `outputs/results/sessions`，含人工决定、异常，随原始录制备份 |
| 完整性与评分 | `outputs/results/integrity`、`scores`，保留输入身份和参数 |
| 优化与派生数据 | `runs`、`data/derived`，按运行名追溯 |
| 回放缓存 | 录制下的 `review/`，确认可重建后才可清理 |
| 现场配置 | `config`、`.env`，保留标定、对齐及部署设置 |

结果以录制路径的稳定 ID 关联，避免同名覆盖。移动到不同绝对路径可能改变身份；旧 basename 报告会保留，但不再混用到列表徽标，必要时对目标重新检查。

### 备份顺序

1. 确认本条已停止并完成保存。
2. 复制整条录制目录、任务/Take 元数据和所需质检结果。
3. 核对副本文件数、大小、SHA-256，并做读取或回放检查。
4. 按保留规则决定是否删除源文件，保留交付清单和异常记录。

清单包含任务、Take、路径或稳定 ID、时间范围、话题、完整性、评分、人工判断、哈希和异常。评分、缓存和发布包均不能替代原始 MCAP。

部署挂载使用 `.env` 的 `INSIGHT_ROSBAG_HOST_DIR`，通过 `INSIGHT_ROSBAG_REQUIRED_SOURCE` 核验实际采集盘。备用存储告警出现时核对真实路径，按现场 SOP 决定是否继续，不通过清空核验项隐藏告警。

### 保存失败时

先确认是否还在写入或收尾，保留原始暂存、错误和诊断。停止请求取消后，后端仍会继续处理收尾；写入器未停止时会阻止新录制和相关删除。由维护人员核对写入器、空间与恢复结果，不通过强行起录绕过保护。

中断恢复后能读取，不等于动作完整、无掉帧或时间正确；仍要做完整性和人工复查，不手改 Take JSON 判通过。

## 6 构建和升级

在适配的开发或发布机执行；现场客户机使用验证过的发布产物。首次准备依赖可能需要网络，已有 builder 可离线构建。

```bash
./native_dashboard/build.sh --prepare-builder
./native_dashboard/build.sh
npm ci --prefix web_dashboard
node web_dashboard/build.js
```

Qt 源码改动需重建原生程序，手机源码改动需重建静态产物，运行依赖变化需重建镜像。在验证过的干净源码目录中，将 `RELEASE_VERSION` 设为本次批准的发布版本，再执行：

```bash
./deploy/build_release.sh "$RELEASE_VERSION"
```

产物为 `release/insight-dashboard-<版本>.run`。发布清单记录发布版本、完整提交、`jetson-nx` 和包 SHA-256。

升级前停止采集和后处理、等待保存、关闭 Qt，备份配置、标定、任务元数据及原始录制。执行实际发布文件，并指定原安装目录：

```bash
bash <实际发布包.run> <原安装目录>
```

尖括号是待替换项。升级会迁移配置、加载镜像并同步 Qt；成功后可能清理其管理范围内的旧发布包和正式镜像。保留现场配置和原始数据，升级后按下一节验收，再恢复正式采集。

## 7 验收与交接

### 每次安装或升级后走完短样本

确认安装身份 → 连接手机和 Qt → 三台设备输入与时钟就绪 → 核对采集盘 → 校准 → 短录制 → 停止保存 → 回放 → 完整性检查 → 人工复查。

后端 healthy、分数高、画面平滑、历史实机记录，都不能单独替代这条流程。时间、曝光同步、文件完整性、定位精度和动作可用性分别判断。

### 本次源码验证范围

| 项目 | 已完成和边界 |
| --- | --- |
| 代码修复 | 23 项普通问题修复已合入主线；未恢复此前停止的深度安全检查 |
| 后端回归 | 571 项通过，4 项因 ROS、Jetson 硬件或 ARM64 条件跳过 |
| QML 与模型 | 行为及语法检查通过；正常模型用现有 Qt5 库编译运行通过 |
| Qt6 和设备 | 缺 Qt6 开发包，异步 C++ 用例及完整 Qt6 构建未运行；本轮未新构建发布包、部署或完成端到端实机验收 |

这些结果描述源码验证，不能写成现场设备已通过验收。

### 交接记录

- 开工：操作者、日期地点、设备标识和佩戴角色、安装版本和提交、配置及现场修改、采集盘路径、容量、输入、时钟及短样本。
- 每条：任务、Take、路径或稳定 ID、动作和有效区间、是否包含校准、完整性、评分、人工决定、异常及原因。
- 收工：最后一条已保存、本批清单、备份位置和副本核验、现场配置与诊断、待复查项和下一班限制。

## 8 只读诊断和源码参考

在 Jetson 项目或安装目录执行。接口返回可响应不代表相机链路和录制文件均正确。

```bash
curl -fsS http://127.0.0.1:8765/healthz
curl -fsS http://127.0.0.1:8765/api/recording/status
curl -fsS http://127.0.0.1:8765/api/cameras
curl -fsS http://127.0.0.1:8765/api/mapping
curl -fsS http://127.0.0.1:8765/api/mobile/status
curl -fsS http://127.0.0.1:8765/api/system/time
docker compose ps
```

以下仅在源码目录适用：

```bash
git status --short
git rev-parse HEAD
git branch -a
```

诊断记录保留时刻、版本、设备、任务、Take、路径、错误和最近操作。Qt 用 **Copy diagnostics**，手机用 **设备 → 诊断 → 保存诊断**。

维护依据为当前主线的 `native_dashboard/run.sh`、`scripts/run_dashboard.sh`、`deploy/run_dashboard.sh`、`deploy/update.sh`，以及 jetson-nx 模板、手机页面、Qt 七页和后端路由。当前操作手册统一从 `docs/USAGE.md` 进入；技术资料见 `native_dashboard/PARITY.md`、`docs/CAMERA_STARTUP.md`、`docs/MOBILE_PREVIEW.md`、`docs/INSIGHT9_SPARSE_MAPPING.md`。通用发布流程见 `docs/DEPLOYMENT.md`，其中配置示例需按本文的 jetson-nx 使用。修复记录见 `docs/CODE_REVIEW_FIXES_20261118.md`。
