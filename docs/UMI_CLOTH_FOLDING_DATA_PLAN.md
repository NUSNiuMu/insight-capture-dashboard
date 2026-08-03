给 Insight3 global localizer 增加左右夹爪静态 mask。
定义左右 TCP 坐标系。
标定 camera_center → TCP。
修复校准工具的 mono8/8UC1 解码。
实现 rosbag 离线 gripper extractor。
生成开合可视化曲线和 marker overlay 视频做人工验证。
将 TCP pose、opening 与三路图像对齐到 20 Hz。
输出双臂 20 维 UMI action。
最后再导出 UMI Zarr/LeRobot。