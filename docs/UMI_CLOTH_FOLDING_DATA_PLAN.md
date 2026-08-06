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
