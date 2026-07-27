# Insight9 稠密双目地图

该验证节点使用校正后的左右红外图计算 StereoSGBM 稠密视差，把有效深度点
通过 100 Hz VIO 和 `T_imu_left` 转换到世界坐标，并按 4 cm 体素累计融合。
它不依赖 PyTorch 或额外模型，也不参与三个相机的重定位。当前默认验证流程
已经改用稀疏 3D 描述子地图，本节点仅保留为可选的场景形状诊断工具。

## 可选启动

```bash
cd /home/nvidia/insight-capture-dashboard
docker compose --profile mapping-validation up -d --force-recreate \
  insight9-dense-mapper
```

`scripts/run_mapping_validation_rviz.sh` 会主动停止该服务，确保默认的三相机
定位验证不消耗 StereoSGBM 的 CPU 和内存。

## RViz 图层

- `Confirmed sparse map`：默认开启，蓝色，是重定位实际使用的确认地标。
- `Insight3 A global path`：洋红色，定位到全图后的 Insight3 A 轨迹。
- `Insight3 B global path`：绿色，定位到全图后的 Insight3 B 轨迹。
- `VIO camera path`：Insight9 本轮建图轨迹。

双 Insight3 全局定位的启动与诊断见
[INSIGHT3_GLOBAL_LOCALIZATION.md](INSIGHT3_GLOBAL_LOCALIZATION.md)。

默认 RViz 配置不包含稠密点云图层。如果临时诊断场景形状，可在 RViz 手动
添加 `/insight9_dense_map/current_points` 或
`/insight9_dense_map/fused_points`。

输出话题：

- `/insight9_dense_map/current_points`
- `/insight9_dense_map/fused_points`
- `/insight9_dense_map/status`

查看状态：

```bash
docker compose exec insight-dashboard bash -lc '
source /opt/ros/humble/setup.bash
ros2 topic echo --no-daemon --full-length \
  /insight9_dense_map/status std_msgs/msg/String
'
```

默认每隔 2 个像素采样，深度范围为 0.25–6 m，以 2 Hz 处理；相机平移
5 cm 或旋转 3°后把当前帧加入累计地图。可通过节点参数调整视差范围、采样
步长、深度范围、体素大小和地图上限。

## 实机基线

2026-07-27 在 Orin NX、544×640 双目红外输入上：

- 当前帧约 43,000 个稠密三维点；
- 第一关键帧约 17,800 个融合体素；
- StereoSGBM、坐标转换和体素处理约 64 ms；
- 节点内存约 86 MiB，运行时约占一个 CPU 核的 78%。

StereoSGBM 在无纹理、反光、遮挡和画面最左侧视差搜索区会产生空洞。累计
地图用于直观验证场景结构，不替代生产级 TSDF、回环或动态物体剔除。
