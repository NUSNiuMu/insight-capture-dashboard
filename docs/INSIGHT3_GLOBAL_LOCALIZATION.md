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
RViz。关闭 RViz 或按 `Ctrl+C` 退出脚本时，会自动停止稀疏 mapper、localizer
和 SuperGlue 推理服务，释放 dashboard 所需的 CPU、GPU 和内存。

Insight9 稀疏节点需要至少三个有重叠视野的关键帧确认地标。启动后缓慢移动
Insight9，平移超过 5 cm 或旋转超过 3°，同时持续观察同一个有纹理区域。
两个 Insight3 也需要看到该区域。地图中至少有 80 个确认特征后才开始定位。

定位并不是单帧跳转。每一路必须连续得到三次相互一致的 PnP 结果，并满足：

- 至少 12 个描述子匹配、10 个 PnP 内点；
- 内点比例至少 45%，中值重投影误差不超过 3 px；
- 内点至少覆盖图像的四个网格区域；
- 连续结果之间小于 20 cm 和 12°。

确认成功后，节点用误差状态 EKF 融合 VIO 和重定位：

- 第一次确认直接初始化 `T_insight9_map_insight3_odom`，让相机立即到达真实位置，
  并从该点开始全局轨迹；
- 后续 VIO 作为 100 Hz 连续运动预测，重定位结果作为低频绝对观测更新
  `T_map_odom`；
- EKF 的后验修正按默认 1 秒时间常数随 VIO 帧连续注入，后续重定位不再清空
  轨迹，也不会在单帧内把相机跳到新位置。

默认平移过程噪声是 `0.02 m/√s`、旋转过程噪声是 `0.5°/√s`；重定位观测标准差
是 `0.10 m` 和 `3°`。可用命令行参数 `--ekf-process-translation-std`、
`--ekf-process-rotation-std-deg`、`--ekf-measurement-translation-std`、
`--ekf-measurement-rotation-std-deg` 和
`--ekf-correction-time-constant-sec` 调整。

PnP 仍在左目光学坐标系中计算，但发布前会通过设备 TF 求出左右目光心中点：

`T_map_center = T_map_odom · T_odom_imu · T_imu_left · T_left_center`

其中 `T_left_center` 的平移是 `T_left_right` 平移的一半，姿态沿用左目。Pose、
Path、轨迹和 Dashboard 模型位置都表示双目中心，不再表示左目位置。
原始 VIO 约为 100 Hz；全局 Pose 以 50 Hz 发布，轨迹按 50 ms 间隔采样，
Path 和相机 TF 以 5 Hz 发布。与 Insight9 相同，每条轨迹最多保留 200 点，
达到上限后旧点随新点进入而逐个消失；按目标采样间隔对应约 10 秒历史。
状态 JSON 独立保持 2 Hz，避免诊断消息占用额外带宽。

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
`ekf_initialized`、`ekf_innovation_translation_m`、
`ekf_innovation_rotation_deg` 和 `ekf_covariance_diagonal` 用于检查融合状态。

输出话题：

- `/insight_global/insight3_a/path`：A 的全局轨迹；
- `/insight_global/insight3_b/path`：B 的全局轨迹；
- `/insight_global/insight3_a/status`、`.../insight3_b/status`：定位诊断；
- `/insight9_sparse_map/features`：Insight9 三维位置和 256 维描述子地图；
- TF `insight9_map -> insight3_a_global_camera_center`；
- TF `insight9_map -> insight3_b_global_camera_center`。

## RViz

RViz 固定坐标系是 `insight9_map`。默认显示蓝色确认稀疏地图、黄色 Insight9
轨迹、洋红色 Insight3 A 全局轨迹和绿色 Insight3 B 全局轨迹。

如果状态停在 `waiting`：

- `image_ready`、`camera_info_ready`、`extrinsic_ready` 应全部为 `true`；
- `map_features` 小于 80 时，继续移动 Insight9 建立重叠关键帧；
- 地图已就绪但匹配不足时，让 Insight3 对准 Insight9 已建图的纹理区域；
- 不要仅为了得到结果降低 PnP 内点或三帧一致性门槛。
