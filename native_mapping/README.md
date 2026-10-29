# Insight9 / Insight3 定位核心

从只读 LooperVIO 工作树迁入持续地标、共视局部 BA、关键帧裁减、回环和几何定位。来源版本、实际修改和文件指纹见 `third_party/loopervio/ORIGIN.json`。保留原版权声明；未编译的 VIO 实现不随迁入代码携带。

线上只有一条路径：SuperPoint/SuperGlue → 双目亚像素细化 → LooperVIO 地标与共视优化 → 腕部几何定位。设备自身 VIO 提供相邻视觉更新之间的运动。没有 ORB 备选路径，也不回用未经多帧验证的原始三维点。

## 几何与状态

- 关键帧固定按时间采样，目标 2 Hz，静止时也建图。地图写入、局部 BA、回环与查询由同一个 C++ 请求线程执行。
- 头部按共享地图中可见地标进行关联和位姿优化；回环在两个独立共视参考的相机位置与旋转一致时建立，修正后以真实重投影检查融合地标。
- 腕部通过几何验证后保留接受的地标身份与局部三维坐标；每次局部 BA 或回环后，估计这些地标变形的刚体分量来更新腕部位姿，不重复检索旧图像。已无足够地标时明确失效。每次地图事务返回同一 `map_revision` 的头部及两腕修正，ROS 层一次加锁提交后才传输地图点云。
- 主定位仅使用至少被三个关键帧观测、且通过多帧重投影检查的地标。保留各个 SuperPoint 特征身份，不按图像网格删除其他有效点。
- 腕部定位采用 RANSAC、1 px Huber 细化、3 px 几何剔除及再次拟合；至少 40 个内点、50% 内点比例、四个图像网格支持后才更新。
- 通过几何检查即更新当前修正。删除对历史 `map_from_odom` 平移量的时间聚类，避免 VIO 坐标原点及 VIO 漂移影响真实视觉修正。
- 三台设备分别保留自己的 VIO 坐标，不能作为刚性三相机架处理。对每台设备：`C = map_from_imu @ inverse(odom_from_imu)`；发布的双目中心为 `C @ odom_from_imu @ imu_from_center`。
- 使用 CameraInfo 的校正投影矩阵与 TF 外参，头部右目由校正基线构造。RGB 评估通过其独立外参换算，不能把 RGB 光心当成双目中心。

ROS 层发布原有三路位姿、状态、地标描述子云与标定参考图，保留手机的开始校准和录制接口。三路在共同的 30 Hz 网格采样，Pose 与 TF 使用同一时间戳；软件时钟偏移估计和地图版本随状态记录。没有新视觉匹配时，状态明确标识运动由 VIO 推算。

## 构建与验证

```bash
docker build --network host -f native_mapping/Dockerfile.dependencies -t insight-loopervio-deps:2.1 native_mapping
docker compose build insight9-sparse-mapper
docker compose up -d --no-deps insight9-sparse-mapper insight3-global-localizer
```

`insight9-sparse-mapper` 运行唯一 C++ 核心，`insight3-global-localizer` 运行三路 ROS 适配层。重建、重置与传输故障均使用这条路径。

在装有构建依赖的镜像内运行真实几何检查：

```bash
PYTHONPATH=. python3 native_mapping/tests/check_geometry.py /usr/local/bin/insight_mapping
```

检查已知空间点的双目建图、BA、两路腕部位姿、错误对应拒绝、普通局部地图更新时的两腕修正、点云传输，以及重置后重新建图。另可构建并运行 `check_pose_graph` 目标，检查非零、非单位旋转的世界原点在位姿图中保持固定，防止未初始化的稳定参考位姿破坏回环约束，并检查腕部参考关键帧在使用期间不会被延迟删除、释放后可裁减。

同包对照在独立进程运行冻结的旧版与新版，使用相同图像、原始 VIO、内参、外参和时间顺序。标定板只用于独立误差计算，不参与建图或拟合算法参数；全部精度统计从录制中的校准完成时刻开始，后续跟踪失败和纠偏尖峰均保留。两版按共同有效观测比较 RMSE、P95、最大值并另报覆盖率。每个原始 VIO 消息收到时选取当时已存在的修正，构造 30 Hz 双目中心位姿，再按图像源时间插值得到评估位姿，避免把图像接收时间误当作源时间。评估同时给出共同标定板中心偏差和相机间相对平移偏差。几何回放与实际运行耗时分开记录。复核脚本、输入清单、版本指纹和逐点结果归档于本次测试目录。
