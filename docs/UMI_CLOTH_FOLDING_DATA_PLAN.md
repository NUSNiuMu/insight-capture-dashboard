给 Insight3 global localizer 增加左右夹爪静态 mask。
左右 TCP 坐标系已定义，见 [UMI TCP 坐标系](UMI_TCP_FRAMES.md)。
Jetson NX 的 camera_center → TCP 实测外参已录入并发布 TF。
修复校准工具的 mono8/8UC1 解码。
实现 rosbag 离线 gripper extractor。
生成开合可视化曲线和 marker overlay 视频做人工验证。
已实现从 rosbag 将 TCP pose、opening 与三路图像对齐到 20 Hz。
已实现双臂 20 维 action 对应的 UMI Zarr 与训练配置导出。
UMI 导出默认保留三路相机各自的原始分辨率；224/384 方形兼容选项固定裁剪水平居中、
底部对齐的最大方形操作区后再等比缩放，避免 portrait 图像形变。
后续按需要增加 LeRobot 导出。
