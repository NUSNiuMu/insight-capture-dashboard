# 收流与定位故障诊断

- Qt 每路分别显示图像输入状态和定位状态。`No image input` 表示后端未收到该图像流；`Pose input lost` 表示 SDK VIO 未更新。定位正常不能替代图像收流证据。
- 后端收到图像、客户端仍不出画面时，视频区域显示解码或连接状态。右上角 FPS 是客户端实际显示帧率，不是相机标称帧率。
- `Waiting for mapping input` 的提示详情列出缺少的 CameraInfo、TF 或图像坐标系；`Waiting for frame pose` 表示缺少图像时刻的位姿；`Inference unavailable` 表示推理服务未完成请求。
- Qt 点击 `Copy diagnostics` 后粘贴给维护人员；手机展开 `Tasks & recordings`，点击 `Save diagnostics` 下载 JSON。诊断包含实际 ROS 域、图像 QoS、每路输入新鲜度、定位状态及最近 100 条状态变化；Qt 另含本机视频管线信息。
- USB 相机网口的索引、地址或上下线状态变化时，仅重建 SDK 输入的 DDS 参与者，保留应用和地图状态。日志中的 `camera_transport/reconnected` 仅表示输入连接重建；各路 `image_input/receiving`、`vio_input/receiving` 才表示对应数据实际恢复。
- 定位继续使用同一套推理与建图算法。暂时缺输入或推理中断不会进入永久停处理状态；不可恢复的处理错误输出完整堆栈后退出，由现有容器监管重启。没有更换 DDS、估计位姿或软件解码的备用路径。

本轮隔离验证覆盖网口延迟出现及重建、CameraInfo/TF 延迟、启动期与运行期推理中断恢复、Qt 实际预览和复制、手机诊断下载。它不替代顾客设备上的现场复验；客户发布镜像和升级包需另外更新。
