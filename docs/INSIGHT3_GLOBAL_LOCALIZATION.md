# Insight3 在 Insight9 地图中的全局定位

该验证链路把 `insight3_a` 和 `insight3_b` 两条独立 VIO 轨迹定位到
Insight9 的 `insight9_map`。它不使用 RTAB-Map：Insight9 双目关键帧生成带
SuperPoint 描述子的三维地标，Insight3 单目图像与这些地标匹配，再通过
PnP 求出相机在全图中的位置。

## 每次新建地图并启动 RViz

```bash
cd /home/nvidia/insight-capture-dashboard
scripts/run_mapping_validation_rviz.sh
```

每次执行该脚本都会停止可选的稠密 mapper，并重建稀疏 mapper 和 localizer，
清空上一会话的稀疏地图、两个 Insight3 的全局校正及三条轨迹，然后才打开
RViz。SuperGlue 推理服务没有地图状态，会保留运行以避免重复加载模型。

Insight9 稀疏节点需要至少三个有重叠视野的关键帧确认地标。启动后缓慢移动
Insight9，平移超过 5 cm 或旋转超过 3°，同时持续观察同一个有纹理区域。
两个 Insight3 也需要看到该区域。地图中至少有 80 个确认特征后才开始定位。

定位并不是单帧跳转。每一路必须连续得到三次相互一致的 PnP 结果，并满足：

- 至少 12 个描述子匹配、10 个 PnP 内点；
- 内点比例至少 45%，中值重投影误差不超过 3 px；
- 内点至少覆盖图像的四个网格区域；
- 连续结果之间小于 20 cm 和 12°。

确认成功后，节点保存固定的 `T_insight9_map_insight3_odom`。定位前缓存的整段
VIO 历史会立即重新变换并发布，而不是从确认时刻另起一条轨迹。
原始 VIO 约为 100 Hz；全局轨迹按 50 ms 间隔采样，并以 20 Hz 发布 Path 和
相机 TF。状态 JSON 独立保持 2 Hz，避免诊断消息占用额外带宽。

## 状态检查

```bash
docker compose exec insight-dashboard bash -lc '
source /opt/ros/humble/setup.bash
ros2 topic echo --full-length /insight9_sparse_map/status
'

docker compose exec insight-dashboard bash -lc '
source /opt/ros/humble/setup.bash
ros2 topic echo --full-length /insight_global/insight3_a/status
'

docker compose exec insight-dashboard bash -lc '
source /opt/ros/humble/setup.bash
ros2 topic echo --full-length /insight_global/insight3_b/status
'
```

定位状态中的 `descriptor_matches`、`inliers`、`inlier_ratio`、
`median_reprojection_error_px` 和 `rejection` 用于判断结果。单个后续帧被拒绝
不会清除已确认的全局变换；只有新的三帧一致结果才会更新它。

输出话题：

- `/insight_global/insight3_a/path`：A 的全局轨迹；
- `/insight_global/insight3_b/path`：B 的全局轨迹；
- `/insight_global/insight3_a/status`、`.../insight3_b/status`：定位诊断；
- `/insight9_sparse_map/features`：Insight9 三维位置和 256 维描述子地图；
- TF `insight9_map -> insight3_a_global_camera_left`；
- TF `insight9_map -> insight3_b_global_camera_left`。

## RViz

RViz 固定坐标系是 `insight9_map`。默认显示蓝色确认稀疏地图、黄色 Insight9
轨迹、洋红色 Insight3 A 全局轨迹和绿色 Insight3 B 全局轨迹。

如果状态停在 `waiting`：

- `image_ready`、`camera_info_ready`、`extrinsic_ready` 应全部为 `true`；
- `map_features` 小于 80 时，继续移动 Insight9 建立重叠关键帧；
- 地图已就绪但匹配不足时，让 Insight3 对准 Insight9 已建图的纹理区域；
- 不要仅为了得到结果降低 PnP 内点或三帧一致性门槛。
