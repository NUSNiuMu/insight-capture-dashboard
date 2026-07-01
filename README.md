# Insight Live Dashboard

这个目录现在同时保留两条 dashboard 主线：

- 现有 Qt 版：实时查看三路相机图像、显示三路 VIO 轨迹，并在 dashboard 内做内存态在线相对位姿对齐
- 新的 Web 版：后端继续复用 ROS2/VIO pose 处理，前端改成 Babylon.js 浏览器 GPU 渲染 avatar

默认使用 `ROS_DOMAIN_ID=20`。

## 单一配置入口

优先只改：

- [config/cameras.json](/home/seeed/workspaces/insight_capture/config/cameras.json:1)

这个文件控制：

- dashboard 显示哪三路图像
- dashboard 使用哪三路 VIO
- 在线对齐使用的 AprilTag board 参数
- 标定输出使用哪个参考系，当前默认是 `board_center`
- 默认 `ROS_DOMAIN_ID`

如果要把相机换成新的命名空间，通常只需要改对应相机的：

```json
"namespace": "insight7_b"
```

如果显示流或 VIO 流也变化，再改：

- `dashboard_image_stream`
- `dashboard_pose_stream`
- `dashboard_label`

## 保留的脚本

- `scripts/open_monitor_dashboard.sh`: 当前 dashboard 启动入口
- `scripts/multi_camera_dashboard_qt.py`: Qt dashboard 主入口，负责 ROS 订阅、图像解码和窗口组装
- `scripts/multi_camera_dashboard_web.py`: Web dashboard 后端，负责 ROS2 pose 订阅、fake-pose demo 和 WebSocket 推流
- `scripts/post_processing.py`: Web 版 rosbag 录制管理、topic 发现与分组
- `scripts/live_alignment.py`: 在线 AprilTag 相对位姿对齐和诊断日志
- `scripts/dashboard_widgets.py`: 图像面板、轨迹控件和轨迹绘制逻辑
- `scripts/camera_setup.py`: 从 `config/cameras.json` 生成 dashboard 所需 topic
- `scripts/session_alignment.py`: 在线对齐使用的位姿/矩阵数学工具
- `config/post_processing.json`: Web 版 rosbag 默认录制配置
- `web_dashboard/`: Babylon.js Web 前端，`npm run build` 后生成静态页面

## 启动 Dashboard

宿主机路径通常是 `/home/seeed/workspaces/insight_capture`。如果已经 `docker exec` 进容器，项目挂载路径是 `/workspaces/insight_capture`。

宿主机：

```bash
cd /home/seeed/workspaces/insight_capture
./scripts/open_monitor_dashboard.sh
```

容器内：

```bash
cd /workspaces/insight_capture
./scripts/open_monitor_dashboard.sh
```

当前 dashboard 默认显示：

- `insight7_a` 的 `color_compressed`
- `insight7_b` 的 `color_compressed`
- `insight9_a` 的 `color_compressed`
- 右侧可旋转缩放的 3D VIO 轨迹

## Web Dashboard

先构建前端：

```bash
cd /home/seeed/workspaces/insight_capture/web_dashboard
npm run build
```

启动 Web 后端：

宿主机：

```bash
cd /home/seeed/workspaces/insight_capture
python3 scripts/multi_camera_dashboard_web.py
```

容器内：

```bash
cd /workspaces/insight_capture
python3 scripts/multi_camera_dashboard_web.py
```

默认会同时提供：

- WebSocket: `ws://localhost:8765/ws`
- pose 快照: `http://localhost:8765/api/poses`
- alignment 状态: `http://localhost:8765/api/alignment`
- recording 状态: `http://localhost:8765/api/recording/status`
- recording topics: `http://localhost:8765/api/recording/topics`
- rosbag 列表: `http://localhost:8765/api/rosbags`

页面入口：

- `http://localhost:8765/` 或 `/3d`: 3D VIO 轨迹页，保留 Babylon.js GPU 场景和在线校准按钮
- `/recording`: 独立 rosbag 录制页，负责 topic 发现、勾选、录制、停止和同步到主机
- `/images`: 图片页骨架，先放高帧率实时看图方案，具体传输实现需要再确认
- `/bags`: 本地 rosbag 列表页，显示路径、大小、时长、label/scoring/optimization 状态
- `/scoring`: 轨迹评分页骨架，下拉选择 rosbag，后端评分 runner 以后再接
- `/optimization`: 轨迹优化页骨架，下拉选择 rosbag，后端 optimizer 以后再接

网页右上角现在也有 `Start Alignment / Stop Alignment` 按钮：

- 不需要 `--start-alignment` 参数也可以随时开始校准
- Web 后端会提前订阅校准所需图像和相机内参
- 适合左边继续看 RGB，右边 3D 页面手动控制开始/停止

网页里也新增了独立的 rosbag Recording 页面：

- `Refresh Topics` 会按当前 `ROS_DOMAIN_ID` 发现 live topics
- topic 会按相机分组显示，支持按组全选/取消
- `Start` 只录制当前勾选的 topics
- `Stop` 会优雅结束 `ros2 bag record`
- 输出目录默认写到 `config/post_processing.json` 里的 `rosbag_dir`

`rosbag_dir` 优先级：

1. CLI: `--rosbag-dir` 或 `-rosbag-dir`
2. 环境变量: `INSIGHT_ROSBAG_DIR`
3. `config/post_processing.json`
4. 默认值: `rosbags`

本地 rosbag 列表页会扫描 `metadata.yaml`，并展示：

- 目录路径
- 递归文件大小
- rosbag duration
- message 数量和 topic 数量
- label 状态：检查 `outputs/results/labels`、`label`、`labeled`
- scoring 状态：检查 `outputs/results/scores`、`scoring`
- optimization 状态：检查 `outputs/results/optimized`、`optimization`

### Images 页面实时看图方案

当前 `/images` 先作为方案页，不直接假装已经实现实时图像。下一步建议三选一：

- 快速版：直接订阅 ROS `CompressedImage`，通过 HTTP/WebSocket 推 JPEG/PNG 快照，开发最快，但高帧率时浏览器解码和带宽压力明显。
- 折中版：WebSocket binary frame + `createImageBitmap`，需要做队列丢帧和 backpressure，适合先把当前 ROS topic 接到页面上。
- 高帧率版：后端编码视频流，前端用 WebRTC 或 WebCodecs 管线，延迟和吞吐更适合实时多相机，但实现复杂度最高。

查过的方向：

- WebRTC 适合实时音视频传输和低延迟媒体通道
- WebCodecs 提供底层视频帧/编码块控制，适合把解码从普通 `<img>` 刷新里拆出来
- OffscreenCanvas 可以把绘制从主线程转移出去
- `createImageBitmap` 可异步解码图像源，适合 WebSocket 二进制图片帧的中间方案

没 ROS2 硬件时可直接跑 demo：

```bash
cd /home/seeed/workspaces/insight_capture
python3 scripts/multi_camera_dashboard_web.py --fake-pose
```

此时浏览器打开 `http://localhost:8765/`，能看到 `head / left_hand / right_hand` 三个节点随 fake pose 运动。

### Web avatar 模型配置

每个 camera 条目支持两个可选字段：

- `avatar_model`: 推荐填相对项目根目录的 `.glb` 或 `.gltf` 路径
- `avatar_scale`: 模型缩放，默认 `1.0`
- `avatar_rotation_deg_xyz`: 模型相对 VIO pose 的本地旋转，单位为度，默认 `[0, 0, 0]`
- `avatar_offset_xyz`: 模型相对 VIO pose 原点的本地平移，使用 dashboard 坐标 `[forward, right, up]`，默认 `[0, 0, 0]`

示例：

```json
{
  "name": "insight9_a",
  "avatar_model": "assets/head.glb",
  "avatar_scale": 0.9
}
```

注意：

- Web 版不会使用 OBJ 做 CPU 解析和逐帧绘制
- 如果配置是 `.obj`，前端会给出 warning，并回退到简单 primitive
- 如果模型缺失或加载失败，也会回退到 sphere/box，不会崩溃

## 在线轨迹对齐

如果相机每次佩戴位置不同，可以在 Qt dashboard 里点击 `Start Live Alignment` / 按 `C`，也可以直接在 Web 3D 页面右上角点击 `Start Alignment`。

当前实现参考 `NUSNiuMu/insight-capture-dashboard` 的做法，使用 `AprilTag GridBoard` 的中心作为参考系：

- `session_alignment.alignment_frame = "board_center"`
- `session_alignment.calibration.method = "board_center"`
- 每台相机都可以单独完成标定，不要求三台相机同时看到同一块板
- 输出的是该相机 `VIO world -> board_center` 的锚定变换，适合单机先标、分批标、最后一起显示

当前默认 AprilTag board 参数：

- `6 x 6` GridBoard
- 单个 AprilTag 边长 `5.5 cm`
- marker 间隔 `1.65 cm`
- 字典 `DICT_APRILTAG_36h11`

单相机场景建议流程：

1. 启动要标定的那一路相机和它自己的 VIO。
2. 让这一路相机稳定看到同一块 AprilTag board 几秒。
3. 相机和标定板都可以运动，但在采样阶段需要持续看到同一块板。
4. dashboard 会自动丢掉离群样本，并在收够一致样本后持续更新这一路相机相对于 `board_center` 的结果。
5. 继续按任意顺序对其它相机重复这个过程，不需要等它自动停。
6. 需要结束时手动点击 `Stop Alignment`；停止后会保留最后一次内存中的对齐结果继续显示轨迹。

如果三台都在线，也可以按任意顺序逐台标定；不再要求严格时间同步。

状态含义：

- `Alignment ON | board 2/3`: 还有相机没有形成有效板位姿
- `Alignment ON | samples 5/12`: 正在累计一致样本
- `Alignment ON | waiting pose`: 板检测成功，但这一路暂时没有匹配到可用 VIO pose
- `Alignment ON | tracking`: 已经在稳定跟踪相对位姿
- `Alignment OFF | locked`: 在线对齐已关闭，保留最后一次结果

在线对齐开启后，终端每秒输出一行简洁状态：

```text
[alignment] CALIBRATED insight7_a | samples=12 board_to_camera=(0.184, 0.092, 0.614)m dashboard_position=(0.614, 0.184, -0.092)m vio_to_board_anchor=(1.203, -0.447, 0.128)m
```

详细诊断日志默认写到：

```text
/tmp/insight_live_alignment.log
```

可用环境变量改路径：

```bash
INSIGHT_ALIGNMENT_LOG=/tmp/my_alignment.log ./scripts/open_monitor_dashboard.sh
```

## 当前命名约定

- `insight7_a`: `/insight7_a/camera/...`
- `insight7_b`: `/insight7_b/camera/...`
- `insight9_a`: `/insight9_a/camera/...`

如果实际命名空间变化，改 [config/cameras.json](/home/seeed/workspaces/insight_capture/config/cameras.json:1) 即可。


容器内快速启动 Web 后端和右侧 3D 窗口：

```bash
cd /workspaces/insight_capture
python3 scripts/multi_camera_dashboard_web.py

cd /workspaces/insight_capture
./scripts/open_web_3d_right.sh
```
