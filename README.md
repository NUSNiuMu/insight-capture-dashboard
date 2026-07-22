# Insight Live Dashboard

实时查看多路相机图像、显示多路 VIO 轨迹，并在 dashboard 内做在线相对位姿标定（AprilTag board）。

> **交付给客户的使用手册在 [docs/USAGE.md](docs/USAGE.md)**（日常操作、采集
> 流程、故障排查诊断树；不含安装——环境由我们出厂配置好）。
> **怎么打包发布镜像 / 给设备升级 / 全新设备怎么部署，见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**
> （开发者打包手册 + 使用者升级手册 + 全新 Jetson 首次部署，两条路径）。
> 内部装机用 `./scripts/setup_host.sh`（幂等：runtime 检查 + DDS 缓冲 sysctl +
> 构建 + 启动）；录制后数据完整性检查用 `scripts/check_bag.py`。
> 本 README 侧重功能与配置参考。

当前 dashboard 使用 **Web 版**（`multi_camera_dashboard_web.py`）：ROS2/VIO 处理在后端，前端是 Babylon.js 浏览器 GPU 渲染，可以本机接显示器看，也可以远程浏览器连。

默认 `ROS_DOMAIN_ID=20`（在 [config/cameras.json](config/cameras.json) 里配置）。

## 快速开始

### 方式一：docker compose（推荐，持久化服务）

```bash
docker compose build   # 首次构建，之后代码不变可跳过
./scripts/run_dashboard.sh            # 只启动后端，打印 SSH 隧道命令，供笔记本电脑远程连
./scripts/run_dashboard.sh --jetson   # 额外拉起本机全屏 3D kiosk，接了显示器的场景用
```

`run_dashboard.sh` 内部就是 `docker compose up -d` + 健康检查，幂等，可以随时重复跑。容器用 `restart: unless-stopped`，SSH 断开、机器重启后 Docker 会自动拉起来。

远程访问：在自己电脑上开 SSH 隧道，不需要暴露端口到局域网：

```bash
ssh -L 8765:localhost:8765 <user>@<jetson-ip>
# 然后浏览器打开 http://localhost:8765/
```

### 方式二：直接跑 Python（开发调试用）

```bash
cd web_dashboard && npm run build   # 改过前端代码才需要重新 build
cd ..
python3 scripts/multi_camera_dashboard_web.py
```

宿主机和容器内命令完全一样，只是工作目录不同（宿主机通常是 `/home/seeed/workspaces/insight_capture`，容器内 `docker exec` 进去是 `/workspaces/insight_capture`）。

没有 ROS2 硬件时可以用 demo 模式：

```bash
python3 scripts/multi_camera_dashboard_web.py --fake-pose
```

浏览器打开 `http://localhost:8765/`，能看到几个 pose 节点随假数据运动。

容器内如果还想在本机同时拉起右侧 3D 窗口（不通过 `run_dashboard.sh` 的话）：

```bash
python3 scripts/multi_camera_dashboard_web.py &
./scripts/open_web_3d_right.sh
```

## 单一配置入口：config/cameras.json

> `config/cameras.json`、`config/board_calibration.json`、`config/post_processing.json`
> 是按设备生成的产物（`.gitignore` 掉了），源头是 `config/devices/<name>/`，用
> `scripts/select_device.sh <name>` 切换/生成——直接改 `config/` 下的文件本身没问题
> （对当前选中的设备生效），但换设备/长期改动要改回 `config/devices/<name>/` 里的源文件，
> 否则下次 `select_device.sh` 会把改动覆盖掉。详见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

**优先只改这一个文件**：[config/cameras.json](config/cameras.json)。它控制：

- dashboard 显示哪几路图像、用哪几路 VIO
- 在线标定使用的 AprilTag board 参数（`calibration_file` 指向 [config/board_calibration.json](config/board_calibration.json)）
- 标定输出参考系（当前默认 `board_center`）
- 默认 `ROS_DOMAIN_ID`

每个 camera 条目的常用字段：

| 字段 | 作用 | 例子 |
|---|---|---|
| `namespace` | 相机的 ROS 命名空间 | `insight3_a` |
| `dashboard_image_stream` | dashboard 显示用哪路图像流 | `infra1` / `color_compressed` |
| `alignment_image_stream` | **在线标定**检测 AprilTag 用哪路流，不填则退回全局 `board_calibration.json` 里的 `image_stream` | `infra1` |
| `dashboard_pose_stream` | VIO pose 流 | `vio_100hz` |
| `teleop_role` | 决定 3D 场景里用哪个位置/朝向预设 | `head` / `left_hand` / `right_hand` |
| `avatar_model` | 3D 场景里这个相机用的模型，见下方"Web avatar 模型配置" | `assets/models/vis_assembly.glb` |

`dashboard_image_stream`/`alignment_image_stream` 的可选值定义在 [camera_setup.py](scripts/camera_setup.py#L9-L15) 的 `IMAGE_STREAMS` 里：`infra1`、`infra2`、`depth`、`color`、`color_compressed`。

**为什么 `alignment_image_stream` 要单独配一个字段**：dashboard 显示和在线标定检测经常需要不同的流——比如同一台相机 dashboard 想看压缩后的彩色图省带宽，但标定检测想用未压缩的原始图提高角点精度；混合机队场景更明显，比如黑白/IR 相机和彩色相机同时在线，标定就不能全局共用一个 stream 名字，必须按相机各自指定。

### 当前命名约定

按 `config/cameras.json` 里实际配置的机队为准，改命名空间/角色直接改这个文件，不需要碰代码。

## 相机重启与网络

```bash
./scripts/reboot_cameras.sh
```

会自动扫描当前活跃的 `169.254.x.x` 网络接口发现相机（每台相机独占一个 USB 网口、独立 `/24` 段），不需要在脚本里写死 IP；换了 segment 或增减相机都不用改脚本。

## Web Dashboard 功能

启动后同时提供：

- WebSocket: `ws://<host>:8765/ws`
- pose 快照: 随 WebSocket 首帧推送（不是独立的 `/api/poses` GET）
- alignment 状态: `http://<host>:8765/api/alignment`
- recording 状态/topics: `http://<host>:8765/api/recording/status`、`/api/recording/topics`
- rosbag 列表: `http://<host>:8765/api/rosbags`
- 健康检查: `http://<host>:8765/healthz`

页面入口：

- `/` 或 `/3d`：3D VIO 轨迹页，Babylon.js GPU 场景 + 在线标定按钮
- `/recording`：独立 rosbag 录制页，topic 发现、勾选、录制、停止、同步到主机
- `/bags`：本地 rosbag 列表页，路径/大小/时长/label/scoring/optimization 状态
- `/scoring`：轨迹评分页骨架
- `/optimization`：COLMAP 轨迹优化页（`jetson-nx` profile 的镜像自带 CUDA sm_87 编译的 COLMAP 3.9.1，开箱即用，见下方"部署"）

3D 页面右上角 `Start Alignment / Stop Alignment` 按钮：不需要命令行参数，随时可以开始/停止标定，适合左边看 RGB、右边控制标定。

Recording 页面：`Refresh Topics` 按当前 `ROS_DOMAIN_ID` 发现 live topic（按相机分组，支持整组勾选），`Start` 只录勾选的 topic，`Stop` 优雅结束 `ros2 bag record`。输出目录优先级：CLI `--rosbag-dir` > 环境变量 `INSIGHT_ROSBAG_DIR` > `config/post_processing.json` > 默认 `rosbags`。

Bags 列表页扫描 `metadata.yaml`，展示目录路径、递归文件大小、duration、message/topic 数量，以及 `outputs/results/{labels,scores,optimized}` 里对应的 label/scoring/optimization 状态。

### Web avatar 模型配置

每个 camera 条目支持：

- `avatar_model`：相对项目根目录的 `.glb`/`.gltf` 路径（不支持 `.obj`，会回退到 primitive；模型缺失/加载失败也会回退，不会崩溃）
- `avatar_scale`：默认 `1.0`
- `avatar_rotation_deg_xyz`：相对 VIO pose 的本地旋转（度）
- `avatar_offset_xyz`：相对 VIO pose 原点的本地平移，`[forward, right, up]`

```json
{
  "name": "insight9_a",
  "avatar_model": "assets/models/iron-man_helmet_mk3_clean.glb",
  "avatar_scale": 0.5
}
```

模型文件走 `/asset?path=...` 接口提供，带 `Cache-Control` 长缓存，重复打开 3D 页面不会重新下载大模型。

## 在线轨迹标定（Live Alignment）

用 `AprilTag GridBoard` 的中心作为参考系，每台相机独立标定，不要求同时看到板子、不要求严格时间同步：

- `session_alignment.alignment_frame = "board_center"`
- `session_alignment.calibration.method = "board_center"`
- 输出该相机 `VIO world -> board_center` 的锚定变换，适合单机先标、分批标、最后一起显示

标定参数在 [config/board_calibration.json](config/board_calibration.json)（当前默认值，按需调）：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `board_rows`/`board_cols` | `6`/`6` | GridBoard 行列数 |
| `marker_length_m` | `0.055` | 单个 marker 边长 |
| `marker_separation_m` | `0.0165` | marker 间隔 |
| `dictionary` | `DICT_APRILTAG_36h11` | AprilTag 字典 |
| `min_detected_tags` | `12` | 低于这个数量的检测结果直接丢弃 |
| `required_samples` | `12` | 攒够这么多"合格"样本才输出结果 |
| `max_translation_std_m`/`max_rotation_std_deg` | `0.04`/`2.5` | 离群点过滤阈值，单帧偏离共识中心超过这个值就丢弃 |
| `image_stream` | `color_compressed` | 全局默认标定流，可被相机自己的 `alignment_image_stream` 覆盖 |
| `alignment_image_scale` | `1.0` | 检测前对图像的缩放系数，可调大以放大后再检测 |

流程：

1. 启动要标定的那一路相机和它自己的 VIO
2. 让相机稳定看到同一块 AprilTag board 几秒（相机和板都可以动，但采样阶段要持续看到同一块板）
3. dashboard 自动丢弃离群样本，攒够 `required_samples` 后持续更新这一路相对 `board_center` 的结果
4. 继续按任意顺序标定其它相机，不需要等前一台自动停
5. 需要结束时点击 `Stop Alignment`；停止后保留最后一次内存中的对齐结果继续显示轨迹

状态含义：

- `Alignment ON | board 2/3`：还有相机没有形成有效板位姿
- `Alignment ON | samples 5/12`：正在累计一致样本
- `Alignment ON | waiting pose`：板检测成功，但暂时没有匹配到可用 VIO pose
- `Alignment ON | tracking`：已经在稳定跟踪相对位姿
- `Alignment OFF | locked`：在线对齐已关闭，保留最后一次结果

终端每秒输出一行状态：

```text
[alignment] CALIBRATED insight7_a | samples=12 board_to_camera=(0.184, 0.092, 0.614)m dashboard_position=(0.614, 0.184, -0.092)m vio_to_board_anchor=(1.203, -0.447, 0.128)m
```

详细诊断日志默认写到 `/tmp/insight_live_alignment.log`，可用 `INSIGHT_ALIGNMENT_LOG` 环境变量改路径。

### 标定排查要点

- 确认板子在视野内：`http://<host>:8765/api/cameras/<name>/frame` 可查看该相机当前实际画面
- marker 成像像素尺寸建议不低于约 60px；分辨率不足时优先缩短拍摄距离，而非依赖 `alignment_image_scale` 放大
- 角点细化方法固定为 `CORNER_REFINE_SUBPIX`（见 `live_alignment.py`），低分辨率画面下不使用 `CORNER_REFINE_APRILTAG`
- IR/黑白相机需确认标定板在对应波段下仍有清晰的黑白对比度
- 黑白/IR 相机 topic 若编码为 `nv12`，`_decode_calibration_message` 已支持该分支，无需额外处理

## 保留的脚本

| 脚本 | 作用 |
|---|---|
| `scripts/run_dashboard.sh` | 统一启动入口，`docker compose up -d` + 健康检查，`--jetson` 额外拉起本机 kiosk 窗口 |
| `scripts/multi_camera_dashboard_web.py` | Web dashboard 后端：ROS2 pose/图像订阅、fake-pose demo、WebSocket 推流、rosbag 录制/查询 API |
| `scripts/open_web_3d_right.sh` | 本机拉起指向 Web 3D 页面的全屏浏览器 kiosk |
| `scripts/post_processing.py` | Web 版 rosbag 录制管理、topic 发现分组、COLMAP 优化 pipeline 调度 |
| `scripts/live_alignment.py` | 在线 AprilTag 相对位姿标定和诊断日志 |
| `scripts/session_alignment.py` | 在线标定用的位姿/矩阵数学工具 |
| `scripts/camera_setup.py` | 从 `config/cameras.json` 生成 dashboard 所需 topic |
| `scripts/reboot_cameras.sh` | 扫描 `169.254.x.x` 网段并批量重启相机 |
| `scripts/gripper_tracking.py` / `gripper_calibrate.py` | 夹爪张合度识别与标定 |
| `scripts/traj_score.py` | 对一份 rosbag 做轨迹评分（命令行工具，`--help` 看参数） |
| `web_dashboard/` | Babylon.js Web 前端源码，`npm run build` 生成 `dist/` 静态页面 |
| `config/post_processing.json` | Web 版 rosbag 默认录制配置（`rosbag_dir` 等） |

## 部署

打包发布镜像、给设备升级、全新设备首次部署的完整步骤见
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**。这里只记设备差异的技术背景：

### 设备与 config profile

三台设备（`jetson-nx`/`lite`/`lite-779`）现在共用同一个 `main` 分支，不再各自维护
一个 git 分支——设备差异收敛到 `config/devices/<name>/` 下的三个文件（见下方"单一
配置入口"一节），用 `scripts/select_device.sh <name>` 选择。只有 `jetson-nx` 会打
正式发布镜像，`lite`/`lite-779` 是开发机专用 profile。

### COLMAP：只在 jetson-nx profile 上开箱即用

COLMAP（3.9.1，CUDA sm_87，GUI 关闭）和 `looper-vio-colmap-handoff` 流水线在
`docker compose build` 时编译/克隆进镜像（见 Dockerfile 的 `colmap-builder`
阶段），不需要任何宿主机挂载或每台设备手工编译——`/optimization` 开箱即用。
只支持 Orin NX（sm_87 单架构编译），不支持 Nano，因此这也是目前唯一会打正式
发布镜像的设备。`lite`/`lite-779` 这两台开发机没有 COLMAP 依赖，`/optimization`
页面不可用。

`looper-vio-colmap-handoff` 钉在固定 commit 上，其上打了两个本地补丁
（COLMAP 3.9.1 的 GPU 参数名、stdbuf 行缓冲让网页日志实时刷新），升级
该仓库 commit 时需要复核补丁是否仍然适用（见 Dockerfile 内注释）。

部署到新机器后，`config/cameras.json`、`config/board_calibration.json`、
`config/post_processing.json`、`config/alignment/live_alignment_state.json`
都是跟**当前这批相机/这台设备**绑定的配置/状态（前三个由 `select_device.sh`
从 `config/devices/<name>/` 生成，不是手改的），换了相机需要重新走一遍标定，
不是复制过去就能直接用。
