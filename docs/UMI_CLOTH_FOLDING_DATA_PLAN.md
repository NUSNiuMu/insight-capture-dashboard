# 叠衣数据集导出状态

当前标准归档格式是 **LeRobot v3（HiFi-UMI profile）**；UMI Zarr 仅用于
兼容既有训练栈。网页从 `/umi-dataset` 启动导出，每个选中的 rosbag 生成
一个独立数据集，源 bag 不会被多个录制静默拼接。

## 已实现

- 左右 TCP 坐标系与手别由 `tcp_frame_id`、`teleop_role` 明确定义，见
  [UMI TCP 坐标系](UMI_TCP_FRAMES.md)。
- Jetson NX 的 `camera_center → TCP` 外参已录入，Insight3 localizer 发布对应
  静态 TF；其他设备 profile 仍需独立标定。
- 支持 `mono8` / `8UC1` 标定图像，提供 rosbag 离线夹爪宽度提取、开合曲线
  和 marker overlay 视频供人工核验。
- TCP pose、夹爪开度和三路图像统一对齐到 20 Hz。
- 状态固定为双臂 20 维：`[right_10d, left_10d]`，每臂为
  `xyz + rot6d + width`；缺失单臂以零值和有效性掩码表达。
- LeRobot 的 action 是下一时刻绝对状态。π0.5 等训练所需的相对动作和输入
  命名属于训练 adapter，不在归档阶段固化。
- 可按约 1 秒停顿自动切 episode，也可保留“一条 rosbag = 一个 episode”。
- 网页当前默认导出 `Original` 原始分辨率；224×224 和 384×384 为显式训练副本选项。
  方形模式先取水平居中、底部对齐的最大正方形，再等比缩放，避免 portrait 图像形变。
- 只有导出器明确判定为质量不合格的 bag 才会重命名为 `fail_<原名>` 并从 Dataset
  页面隐藏，源数据不会自动删除；配置、标定、系统或导出错误保留原名，避免把环境问题
  误当成坏数据。

## 当前限制与现场检查

- `insight3_a` 与 `insight3_b` 当前使用同一组实测夹爪像素/米制宽度标定；双臂导出
  仍会在任一路标定缺失或无效时安全失败，不会用伪造宽度生成可训练数据。
- 采集前确认两路 `teleop_role`、TCP 静态 TF、全局 pose 和 gripper width 均
  有效；导出后抽查视频、episode 边界、有效性掩码和 Parquet 元数据。

## 三相机离线手部伪标

录制同时包含两路 Insight3 腕部红外、Insight9 头戴彩色、三路 `CameraInfo`、
三路全局 pose 和 `/tf_static` 时，可用 WiLoR 对三路图像逐帧离线提取 21 个手部
关键点，并在 `insight9_map` 中做跨视角一致性检查：

```bash
docker exec -w /workspaces/insight_capture insight-dashboard sh -lc \
  'PYTHONPATH=scripts python3 -m handpose.extract_multiview_wilor \
  rosbags/<bag_name> outputs/handpose/<bag_name>/wilor_multiview \
  --model-dir /opt/insight/models/wilor'
```

输出包含：

- `manifest.json`：B 类离线伪标来源、模型版本、置信度定义、21 点顺序、坐标系、
  单位、相机内外参与失败处理；
- `cameras/<camera>.json`：每个源图像帧的左右手 valid、YOLO bbox confidence、
  2D 像素关键点、相机光学系/`insight9_map` 3D 关键点、相机系/map 系腕部
  `xyzw` 四元数，以及 MANO 原生 15 关节 axis-angle；未检出帧保留为空 hands，
  而不是静默删除；
- `overlays/<camera>.mp4`：三路逐帧骨架和 bbox，供投影目检；
- `quality.json`：逐路检出率、双手检出率、pose 对齐间隔，以及最近时间戳下的
  跨视角 MPJPE 与双向重投影误差。

坐标链固定为：

```text
T_map_image = T_map_camera_center * T_camera_center_image
p_map = T_map_image * p_camera
```

Insight3 的图像是左目光学系，`camera_center` 是左右目中点且方向沿左目；Insight9
彩色相机还会使用录制在 `/tf_static` 中的 `left -> rgb` 外参。不能忽略半基线平移，
也不能把 camera-space WiLoR 点直接与全局 TCP 比较。

三路结果只能作为伪标互证，不是 GT：WiLoR 单目深度存在视角相关尺度偏差，腕部输入
还是红外灰度域。应保留原始逐路结果，利用跨视角误差筛选/降权，不应直接平均成真值。
WiLoR 权重为 CC-BY-NC-ND，MANO 另有非商业研究许可，商用交付前必须重新确认许可。
