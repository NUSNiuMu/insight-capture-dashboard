# Insight9 在线稀疏建图验证

该验证节点使用 Insight9 校正后的左右红外图、100 Hz VIO 和 Magic Leap 官方
SuperPoint/SuperGlue，在当前会话内建立稀疏地图，并发布 RViz 点云与相机轨迹。

## 当前边界

- 仅用于内部技术验证，不进入客户发布镜像。
- 官方代码及权重固定到提交
  `ddcf11f42e7e0732a0c4607648f9448ea8d73590`。官方许可将使用主体和用途限制为
  符合条件的非商业内部研究，并禁止向第三方分发；使用前仍需由项目方确认适用性。
- `Dockerfile.superglue-validation` 基于固定 digest 的 NVIDIA
  `25.04-py3-igpu`，并将固定版本的官方代码及权重直接构建进内部验证镜像。
- 不安装临时 PyTorch wheel。固定 digest 的 NVIDIA 容器永久提供匹配的
  PyTorch、CUDA、ONNX 和 TensorRT 10.9 运行环境。
- 默认推理后端已经切换为 TensorRT：SuperPoint 使用 FP16，数值敏感的
  SuperGlue/Sinkhorn 使用 FP32。ROS 同步、几何、地图生命周期和 RViz 接口
  不依赖具体推理后端；需要排障时可显式设置
  `SUPERGLUE_BACKEND=pytorch` 回退。
- `/insight9_a/camera/hand_keypoints` 使用彩色相机坐标系，不能直接作为红外图像
  遮罩。当前通过至少三个关键帧的世界坐标一致性过滤移动人手和物体。

## 数据链

```text
左右 mono8 图像
  -> 20 ms 近时同步
  -> 官方 SuperPoint + SuperGlue
  -> 极线、视差、深度和重投影过滤
  -> 双目三角化
  -> 100 Hz VIO 插值到图像时间戳
  -> T_world_imu * T_imu_left
  -> 世界坐标体素确认
  -> PointCloud2 + Path + TF
```

稳定地图点必须在三个不同关键帧落入同一个 4 cm 体素。候选点超过 12 个关键帧
未再次观测会被删除，避免单帧动态物体永久进入地图。

## 构建和启动官方 GPU 验证服务

确认内部研究用途符合官方许可后，构建并启动：

```bash
docker compose --profile mapping-validation build superglue-inference
docker compose --profile mapping-validation up -d \
  superglue-inference insight9-sparse-mapper
```

验证镜像包含：

- NVIDIA Jetson PyTorch `25.04-py3-igpu`；
- 官方 SuperPoint/SuperGlue 提交 `ddcf11f...`；
- `superpoint_v1.pth`；
- `superglue_indoor.pth`；
- `superglue_outdoor.pth`。

构建阶段会校验三份权重的 SHA-256；任一内容不一致即失败。该镜像名为
`insight-superglue-validation:25.04`，禁止推送到镜像仓库或写入
`scripts/build_release.sh`。

首次启动会从固定官方权重导出 ONNX，并在当前 Jetson 上编译
`superpoint_dense_fp16.plan` 和 `superglue_fp32.plan`。引擎绑定 TensorRT、
CUDA、GPU compute capability、输入规格和模型参数，校验结果及 SHA-256
写入 manifest；不匹配时自动重建。生成物保存在 Docker volume
`superglue-engines`，后续重启直接加载，不使用临时 wheel，也不把设备专用
plan 提交到 Git。

dashboard 和映射容器使用 host IPC。这是 Fast DDS 跨容器及宿主机 RViz
传输所需：只有 host network 时可以发现话题，但共享内存数据端点在另一个
IPC 命名空间中不可达。

查看启动状态：

```bash
docker compose --profile mapping-validation ps
docker compose --profile mapping-validation logs -f \
  superglue-inference insight9-sparse-mapper
```

首次构建 RViz 验证镜像：

```bash
docker compose --profile mapping-validation build insight9-mapping-rviz
```

每次清空旧地图并打开 RViz：

```bash
scripts/run_mapping_validation_rviz.sh
```

脚本保持在前台，关闭 RViz 后自动收回临时 X11 授权。它会先重建 mapper 和
localizer，确保不继续显示上一会话的内存地图。
当前 RViz 验证配置只显示稀疏确认地图和三条全局轨迹，不启动稠密 mapper。

主要输出：

- `/insight9_sparse_map/points`：经过多关键帧确认的稀疏地图。
- `/insight9_sparse_map/features`：确认地标的三维位置和 256 维 SuperPoint 描述子。
- `/insight9_sparse_map/path`：最多 200 点的左目 VIO 轨迹。
- `/insight9_sparse_map/status`：匹配数、三角化数、稳定点数和处理耗时 JSON。
- TF `insight9_map -> insight9_mapping_camera_left`：独立命名，避免与设备 TF 多父冲突。

查看状态：

```bash
ros2 topic echo /insight9_sparse_map/status
```

停止验证服务：

```bash
docker compose --profile mapping-validation stop insight9-sparse-mapper superglue-inference
```

用该特征地图定位两路 Insight3 的方法见
[INSIGHT3_GLOBAL_LOCALIZATION.md](INSIGHT3_GLOBAL_LOCALIZATION.md)。

## 第一轮验收

- 左右图时间差不超过 20 ms。
- 稳定场景每关键帧有可重复的几何有效点。
- 点云随相机移动保持在世界坐标中，不跟随相机漂移。
- 人手或移动物体的单帧点不进入稳定点云。
- VIO 时间戳回退时自动清空当前会话地图和轨迹。
- 记录 `stereo_matches`、`triangulated`、`confirmed` 和
  `inference_and_geometry_ms`，再判断 5 Hz 目标是否成立。

## 2026-07-27 实机结果

Jetson Orin NX、544×640 双目红外输入、1024 个最大关键点、官方 indoor
权重下：

- TensorRT 10.9 引擎缓存校验为 SM 8.7，SuperPoint FP16 纯引擎平均
  10.68 ms；
- SuperGlue 256/512 点 FP16 纯引擎分别为 47.25/60.05 ms，但 FP16
  Sinkhorn 在精度对照中明显漏配，因此没有采用；
- 最终使用 SuperPoint FP16 + SuperGlue FP32。在固定平移纹理对照中，
  PyTorch/TensorRT 都得到 21 条匹配，19 条完全相同；
- 实时帧样本为 202–211 个 SuperGlue 匹配、152–157 个有效三角化点；
- TensorRT 实时链路样本得到 299 个双目匹配、230 个三角化点，后端推理
  286.16 ms，含几何处理 318.80 ms；
- 强制静止画面连续关键帧后，170 个关键帧形成 976 个已确认地图点；
- 发布点云宽度达到 982，路径保持在配置的 200 点上限；
- dashboard 容器已实际收到 mapper 容器发布的完整状态消息。

结论：默认链路已由 TensorRT 执行，且没有为了速度接受 SuperGlue FP16
精度回归。当前共享 GPU 负载下端到端吞吐仍未达到稳定 5 Hz；下一步性能优化
应针对 SuperGlue 的受约束混合精度和并发 GPU 负载，不能直接把整个 Sinkhorn
降为 FP16。世界坐标静态性、动态物体过滤和长时漂移仍需移动相机采集后验收。
