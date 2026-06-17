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
- 默认 `ROS_DOMAIN_ID`

如果要把相机换成新的命名空间，通常只需要改对应相机的：

```json
"namespace": "insight7_b"
```

如果显示流或 VIO 流也变化，再改：

- `dashboard_image_stream`
- `dashboard_pose_stream`
- `dashboard_label`
- `dashboard.trajectory.pose_qos_reliability`

## 保留的脚本

- `scripts/open_monitor_dashboard.sh`: 当前 dashboard 启动入口
- `scripts/multi_camera_dashboard_qt.py`: Qt dashboard 主入口，负责 ROS 订阅、图像解码和窗口组装
- `scripts/multi_camera_dashboard_web.py`: Web dashboard 后端，负责 ROS2 pose 订阅、fake-pose demo 和 WebSocket 推流
- `scripts/live_alignment.py`: 在线 AprilTag 相对位姿对齐和诊断日志
- `scripts/dashboard_widgets.py`: 图像面板、轨迹控件和轨迹绘制逻辑
- `scripts/camera_setup.py`: 从 `config/cameras.json` 生成 dashboard 所需 topic
- `scripts/session_alignment.py`: 在线对齐使用的位姿/矩阵数学工具
- `web_dashboard/`: Babylon.js Web 前端，`npm run build` 后生成静态页面

## 启动 Dashboard

```bash
cd /home/seeed/workspaces/insight_capture
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

```bash
cd /home/seeed/workspaces/insight_capture
python3 scripts/multi_camera_dashboard_web.py
```

如果默认 `0.0.0.0:8765` 被占用，或者你想只绑定到某个本地网卡/IP，可以改成：

```bash
python3 scripts/multi_camera_dashboard_web.py --host 127.0.0.1 --port 8766
python3 scripts/multi_camera_dashboard_web.py --host 192.168.1.20 --port 8765
python3 scripts/multi_camera_dashboard_web.py --host 127.0.0.1 --port 0
```

也支持环境变量：

```bash
INSIGHT_DASHBOARD_HOST=192.168.1.20 INSIGHT_DASHBOARD_PORT=8766 python3 scripts/multi_camera_dashboard_web.py
```

默认会同时提供：

- WebSocket: `ws://localhost:8765/ws`
- pose 快照: `http://localhost:8765/api/poses`
- 构建后的前端页面: `http://localhost:8765/`

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

轨迹订阅 QoS 也支持单独配置：

```json
"dashboard": {
  "trajectory": {
    "pose_qos_reliability": "best_effort"
  }
}
```

当 VIO 发布端是 `BEST_EFFORT` 时，这个字段需要和发布端匹配，否则 topic 虽然存在，dashboard 也可能收不到 pose。

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

如果三台相机每次佩戴位置不同，直接在 dashboard 里点击 `Start Live Alignment`，或者按 `C`。

当前默认 AprilTag board 参数：

- `6 x 6` GridBoard
- 单个 AprilTag 边长 `5.5 cm`
- marker 间隔 `1.65 cm`
- 字典 `DICT_APRILTAG_36h11`

建议流程：

1. 三台设备和 VIO 都先启动。
2. 让三台相机同时稳定看到同一块 AprilTag board 几秒。
3. 三台相机和标定板都可以运动，但需要三台在同一时段持续看到同一块板。
4. dashboard 会自动丢掉离群样本，并在收够一致样本后开始稳定跟踪相对位姿。
5. 停止在线对齐后，会保留最后一次内存中的对齐结果继续显示轨迹。

状态含义：

- `Alignment ON | board 2/3`: 还有相机没有形成有效板位姿
- `Alignment ON | sync`: 三台都看到了，但时间戳跨度太大
- `Alignment ON | samples 5/12`: 正在累计一致样本
- `Alignment ON | tracking`: 已经在稳定跟踪相对位姿
- `Alignment OFF | locked`: 在线对齐已关闭，保留最后一次结果

在线对齐开启后，终端每秒输出一行简洁状态：

```text
[alignment] tags insight7_a=12 insight7_b=9 insight9_a=10 | seen=insight7_a,insight7_b,insight9_a | usable=insight7_a,insight7_b,insight9_a | Alignment ON | samples 5/12
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
