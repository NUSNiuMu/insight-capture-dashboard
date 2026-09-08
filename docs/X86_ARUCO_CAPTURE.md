# x86 ArUco 数采工作台

独立入口：`python3 -m insight_capture.aruco_capture.app`。不启动原 Dashboard、SuperPoint/SuperGlue、建图器或夹爪 VIO。算法不要求 CUDA/GPU，4090 无需参与首版定位。

## 启动

在 Ubuntu x86 主机安装 Docker 和 Compose，然后在本分支根目录执行：

```bash
docker compose -f deploy/aruco/compose.yml up --build -d
```

浏览器访问 `http://localhost:8770`；其他电脑访问部署主机的局域网地址、端口 `8770`。

```bash
docker compose -f deploy/aruco/compose.yml logs --tail=100
docker compose -f deploy/aruco/compose.yml down
```

没有 Compose/Buildx 插件时，也可直接运行：

```bash
DOCKER_BUILDKIT=0 docker build --network host -f deploy/aruco/Dockerfile -t insight-aruco-capture:local .
mkdir -p outputs/aruco-capture
docker run -d --name insight-aruco-capture --network host --init \
  --restart unless-stopped --stop-signal SIGINT \
  -e RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
  -v "$PWD/config/devices/x86-aruco/capture.json:/app/config/devices/x86-aruco/capture.json:ro" \
  -v "$PWD/outputs/aruco-capture:/app/outputs/aruco-capture" \
  insight-aruco-capture:local
```

下载较慢时可在构建命令中添加 `--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`，仅影响镜像构建，不修改主机全局 pip 配置。

停止用 `docker stop insight-aruco-capture`，再次启动用 `docker start insight-aruco-capture`，日志用 `docker logs --tail=100 insight-aruco-capture`。代码修改后重新构建镜像并重建容器，录制数据保留在宿主机目录；配置修改只需停录后重启容器。

这是独立服务和端口，不需要切换原 Dashboard 的 live profile。已有 Python/ROS 环境也可以直接运行：

```bash
source /opt/ros/humble/setup.bash
python3 -m insight_capture.aruco_capture.app --config config/devices/x86-aruco/capture.json
```

依赖见 `deploy/aruco/Dockerfile`。镜像使用 ROS Humble/Ubuntu 22.04 的多架构基础镜像，不包含 Jetson、TensorRT、模型权重或浏览器。相机发现使用 host 网络；设备须与主机网络互通，`ros_domain_id` 须与 Insight9 一致。

## 配置输入

唯一入口为 `config/devices/x86-aruco/capture.json`：

- `head`：Insight9 已校正 RGB、CameraInfo、原始 VIO 话题，以及 IMU/RGB TF frame。没有内参、外参或新鲜 VIO 时不能起录，不用单位变换占位。
- `arms`、`tcp`、`cube_marker_relative_localization.targets`：左右手标记编号、几何及既有硬件外参。沿用当前硬件，无须夹爪相机上报 VIO。
- `rgb`：附加相机列表。默认两个 ROS 话题只是待接线占位，应替换为真实话题；无对应设备时显示缺失并降低有效率。增加两项即可扩到四路。
- `serial`：默认空，编码器未接入时开合度无效，不会导出伪造宽度。
- `fps`：导出与 QC 的统一采样率，默认 20；原始输入按实际收到的数据保存。

USB RGB 示例，替换 `rgb` 中对应项：

```json
{"name":"rgb_left","device":"/dev/video0","width":1280,"height":720,"fps":30}
```

原始 ROS Image 示例：

```json
{"name":"rgb_left","topic":"/rgb_left/image_raw","compressed":false}
```

串口规格尚未确定，首版只约定一个容易修改的文本协议：每行 JSON，例如 `{"left":1234,"right":5678}`，可只包含一侧。配置线性标定将编码值换算为米：

```json
{"device":"/dev/ttyUSB0","baudrate":115200,
 "left":{"scale_m":0.00001,"offset_m":0,"max_width_m":0.083},
 "right":{"scale_m":0.00001,"offset_m":0,"max_width_m":0.083}}
```

上面比例仅为协议示例，必须替换为实际标定。也可用两个串口分别发送左右值。如果设备直接发送米，比例可设为 1。USB/串口设备还需在 Compose 的 `devices` 中显式映射。硬件协议确定后只修改 `sources.py` 的读取函数。

## 采集与标注

1. 确认头部标定、图像和 VIO 就绪，检查左右 cube 位姿，开始录制。
2. 停录后进入“回放与标注”。播放、暂停、逐帧、倍速和拖动时间轴都会同步各路视频、3D TCP 与开合度。按空格播放/暂停，左右方向键逐帧，I/O 设置片段起止点。
3. 设置片段起止时间、任务文本和参与手臂（左手、右手、双手），添加或编辑片段后保存全部标注。片段不允许重叠；切换录制或关闭页面前提示未保存修改。
4. “质量与导出”显示各维度有效率、缺失时段、20 维详细统计以及可导出片段/帧数。点击缺失区间回到相应时刻检查；预估和最终导出共用有效区间计算。

工作台使用深色布局，左手蓝色、右手橙色。实时页显示输入接收频率、相机/VIO/内外参状态、cube 标记与重投影误差、TCP 坐标和串口开合度。无数据时明确显示等待或无效，不填充演示数值。

3D 轨迹使用仓库已有 Babylon.js，在本地提供资源，网页无需访问 CDN。支持旋转、平移、缩放、适配、俯视/正视/侧视和全屏；坐标单位米，网格间距 0.1 m，TCP 附带 XYZ 朝向轴。无效帧和超过 0.3 秒的间隔会断开轨迹。实时轨迹保留约 4 分钟、最多 2400 次观测，开始新录制或 VIO 重置时清空旧参考系轨迹；离线回放显示整次录制轨迹，长录制只抽稀绘制点，不改变原始数据和 QC。

视频预览为按需 JPEG，实时界面最高约 5 次更新/秒；回放按浏览器请求与解码速度显示，同一批视频解码完成后更新对应的 3D 位姿，不能把网页显示速率当作输入帧率。采集和导出使用原始输入与配置采样率。

原始数据位于 `outputs/aruco-capture/<录制编号>/`：

- `capture.sqlite3`：图像载荷、头部 VIO、左右 TCP 检测及串口原始数值。ROS 回调只排队，写盘和检测在一个独立线程。
- `session.json`：配置、标定、参考变换、时长及错误记录。
- `segments.json`：人工确认的时间范围、任务文本及 `hands` 参与手臂；旧标注默认 `both`。
- `quality.json`：全录制预期时间轴上的有效帧占比。
- `lerobot/`：导出的 Parquet、MP4 和 v3 元数据，包含原始录制与标注溯源。重复导出不会覆盖旧目录。

停录会排空已入队的数据并提交数据库。异常中断的会话保留原始 SQLite，首版不自动恢复或导出未正常结束的录制。

## 位姿与有效性

每次录制以**第一帧能够与头部 VIO 同步的 RGB 所对应的 IMU 位姿**建立固定参考系。双臂共用该参考系；不同录制的原点独立。

```text
T_reference_TCP = inverse(T_world_IMU_start)
                × T_world_IMU_at_image
                × T_IMU_RGB
                × T_RGB_cube
                × T_cube_camera_center
                × T_camera_center_TCP
```

头部 VIO 在图像源时间戳处插值，不外推；其累计漂移不会被 cube 定位消除。检测到头部时间回退或大幅 VIO 跳变后，本次录制后续位姿标为无效，须停止并重新录制。

单面标记允许定位：IPPE 候选先通过深度及重投影检查，误差接近时参考近期全局 cube 位姿选择，再检查运动跳变。没有时间先验的首帧和遮挡后重现仍有平面多解风险；不把低重投影误差当作精度证明。标记不可见不保持上一帧作为有效位姿。

RGB/串口跨设备同步首版使用**主机接收时间**最近邻，默认容差 40 ms；保留 ROS 源时间戳供检查。它不是硬件同步，USB 缓冲、串口延迟及链路抖动要等具体设备到位后实测。队列溢出有计数，所有缺失时间段仍计入 QC 分母。同一张源图像最多对应一个有效输出帧，不把重复补帧计作新的有效采样。

状态为 `[left_10d, right_10d]`，每臂 `xyz + rotation_6d（旋转矩阵前两行）+ width_m`。导出仅保留人工标注范围内、所有配置视频与双臂状态同时有效的连续区间，不连接缺失帧前后的轨迹。`action` 是下一采样时刻的绝对状态；区间最后一帧只作为动作目标，不单独输出训练行。每段至少连续三帧观测，产生至少两行训练数据。

按单次录制导出，不包含自动语义标注、自动任务切分、训练任务、多用户权限、云服务和发布升级。参与手臂仅作为语义元数据写入 episode 的 `participating_hands`，仍要求全部配置视频和双臂状态有效。质量报告、3D 轨迹和回放使用同一套对齐数据。

## 代码位置

`insight_capture/aruco_capture/` 中：`pose.py` 定位，`sources.py` 输入，`capture.py` 录制，`dataset.py` 对齐/QC/导出，`app.py` 提供接口，`index.html` / `workbench.css` / `workbench.js` 提供工作台，`trajectory.js` 封装三维绘制。复用现有 cube 几何、VIO 插值、视频写入及 LeRobot 元数据工具，不复制原 Dashboard。

## 界面设计参考

本次界面设计采用 [Anthropic frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design) 的设计流程，并按 [Vercel Web Interface Guidelines](https://vercel.com/design/guidelines) 检查键盘操作、标签、焦点、空状态、未保存修改保护和响应式布局。技能用于开发参考，不作为运行时依赖。
