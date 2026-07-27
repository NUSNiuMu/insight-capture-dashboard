# Insight9 稠密双目地图

该验证节点使用校正后的左右红外图计算 StereoSGBM 稠密视差，把有效深度点
通过 100 Hz VIO 和 `T_imu_left` 转换到世界坐标，并按 4 cm 体素累计融合。
它不依赖 PyTorch 或额外模型。

## 启动

```bash
cd /home/nvidia/insight-capture-dashboard

docker compose --profile mapping-validation up -d \
  insight9-sparse-mapper insight9-dense-mapper
```

稀疏节点在这里保留轨迹和 TF；RViz 默认关闭稀疏点，显示稠密融合地图。

从 TTY/SSH 启动 RViz 时：

```bash
export DISPLAY=:0
export XAUTHORITY=/run/user/1000/gdm/Xauthority
xhost +si:localuser:root
trap 'xhost -si:localuser:root' EXIT

docker compose --profile mapping-validation run --rm \
  insight9-mapping-rviz
```

## RViz 图层

- `Dense fused map`：默认开启，蓝色，世界坐标中的累计稠密地图。
- `Dense current frame`：默认关闭，红色；需要检查单帧深度形状时开启。
- `Confirmed sparse map`：默认关闭，可与稠密地图对照。
- `VIO camera path`：相机轨迹。

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
