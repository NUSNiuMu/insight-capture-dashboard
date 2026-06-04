# Insight Camera Data Capture

这个目录现在只保留 3 条主链路：

- 固定 topic 录包
- `PoseStamped -> nav_msgs/Path`
- dashboard 实时看三路图像和轨迹

默认使用 `ROS_DOMAIN_ID=20`。

## 单一配置入口

以后优先只改：

- [config/cameras.json](/home/seeed/workspaces/insight_capture/config/cameras.json:1)

这个文件现在同时控制：

- 录包 topic 清单
- `PoseStamped -> Path` 的输入输出
- dashboard 显示哪三路图像
- dashboard 使用哪三路 VIO
- 默认 `ROS_DOMAIN_ID`

如果你要把 `insight3_b` 换成 `insight7_b`，通常只需要改这一项：

```json
"namespace": "insight7_b"
```

如果这台新相机的显示流类型也不同，再顺手改：

- `record_streams`
- `dashboard_image_stream`
- `dashboard_label`

## 目录结构

- `bags/`: rosbag 输出目录
- `config/cameras.json`: 轨迹累计配置
- `scripts/record_camera_topics.sh`: 固定录包脚本
- `scripts/camera_setup.py`: 从统一配置生成录包/轨迹/dashboard 所需内容
- `scripts/pose_to_path.py`: 将 `PoseStamped` 轨迹累计为 `nav_msgs/Path`
- `scripts/run_path_visualizer.sh`: 启动轨迹累计节点
- `scripts/multi_camera_dashboard.py`: 实时图像 + 轨迹 dashboard
- `scripts/open_monitor_dashboard.sh`: 启动 dashboard

## 1. 录包

```bash
cd /home/seeed/workspaces/insight_capture
./scripts/record_camera_topics.sh
```

预览固定录制清单：

```bash
DRY_RUN=1 ./scripts/record_camera_topics.sh
```

指定输出目录：

```bash
./scripts/record_camera_topics.sh /home/seeed/workspaces/insight_capture/bags/test_run
```

当前脚本是固定 topic 清单，不再动态探测。  
目前已知为了保证三相机图像帧率稳定，所有 `depth` topic 都保持注释状态。

如果后续 topic 改名，直接修改 [record_camera_topics.sh](/home/seeed/workspaces/insight_capture/scripts/record_camera_topics.sh:1) 里的 `TOPICS` 数组。

## 2. 轨迹累计

```bash
cd /home/seeed/workspaces/insight_capture
./scripts/run_path_visualizer.sh
```

这个节点会把 [config/cameras.json](/home/seeed/workspaces/insight_capture/config/cameras.json:1) 里的 `pose_topic` 累积成 `path_topic`。

## 3. Dashboard

```bash
cd /home/seeed/workspaces/insight_capture
./scripts/open_monitor_dashboard.sh
```

当前 dashboard 默认显示：

- `insight7_a` 的 `color`
- `insight7_b` 的 `color`
- `insight9_a` 的 `color`
- 右侧可旋转缩放的 3D VIO 轨迹

相关配置也来自 [config/cameras.json](/home/seeed/workspaces/insight_capture/config/cameras.json:1)。

## 4. 当前命名约定

- `insight7_a`: `/insight7_a/camera/...`
- `insight7_b`: `/insight7_b/camera/...`
- `insight9_a`: `/insight9_a/camera/...`

如果实际命名空间变化：

- 录包改 [record_camera_topics.sh](/home/seeed/workspaces/insight_capture/scripts/record_camera_topics.sh:1)
- 轨迹改 [config/cameras.json](/home/seeed/workspaces/insight_capture/config/cameras.json:1)
- dashboard 改 [config/cameras.json](/home/seeed/workspaces/insight_capture/config/cameras.json:1)
- 三台设备都会提供一个 `PoseStamped` VIO topic 用于轨迹显示

如果你的 `insight9` 实际 topic 结构不同，我下一步可以直接继续帮你改成自动适配版本。
