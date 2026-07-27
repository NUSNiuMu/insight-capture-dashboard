# Insight9 稠密双目地图

该验证节点使用校正后的左右红外图计算 StereoSGBM 稠密视差，把有效深度点
通过 100 Hz VIO 和 `T_imu_left` 转换到世界坐标，并按 4 cm 体素累计融合。
它不依赖 PyTorch 或额外模型。

## 每次新建地图并启动 RViz

```bash
cd /home/nvidia/insight-capture-dashboard
scripts/run_mapping_validation_rviz.sh
```

脚本每次都会强制重建稀疏 mapper、稠密 mapper 和双 Insight3 定位节点，因此
上一会话仅存在于内存中的融合点云、关键帧、全局校正和轨迹都会先清空。
SuperGlue 推理容器不保存地图，为避免重复加载模型会继续复用。脚本自动设置
当前桌面的 X11 授权，并在 RViz 退出后收回授权。

不要先执行普通的 `docker compose up -d` 再期待地图自动清空：Compose 会
复用已经运行的容器，内存地图也会继续保留。

## RViz 图层

- `Dense fused map`：默认开启，蓝色，世界坐标中的累计稠密地图。
- `Dense current frame`：默认关闭，红色；需要检查单帧深度形状时开启。
- `Confirmed sparse map`：默认关闭，可与稠密地图对照。
- `Insight3 A global path`：洋红色，定位到全图后的 Insight3 A 轨迹。
- `Insight3 B global path`：绿色，定位到全图后的 Insight3 B 轨迹。
- `VIO camera path`：Insight9 本轮建图轨迹。

双 Insight3 全局定位的启动与诊断见
[INSIGHT3_GLOBAL_LOCALIZATION.md](INSIGHT3_GLOBAL_LOCALIZATION.md)。

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
