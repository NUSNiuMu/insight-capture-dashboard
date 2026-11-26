# 相机组合自动识别与切换

Jetson 自动发现设备，连续两轮识别到唯一一套完整组合后才准备切换。每套需要一个头部设备和两个腕部设备。开始录制、录制中、保存、合包及回放期间等待，空闲时再次核对设备并应用配置。相同组合、相同地址重插时沿用当前配置，视频节点由采集服务重新打开。

| 组合 | 设备身份 |
| --- | --- |
| Insight9 + 双 Insight3 | `insight9_a`、`insight3_a`（右）、`insight3_b`（左） |
| EgoHalo + 双 Insight3 | EgoHalo 采集服务声明的型号，以及同样两个 Insight3 namespace |
| EgoHalo + 双 Insight1 | Insight1 固件型号接口，以及 `insight1_a`（右）、`insight1_b`（左）。Jetson 在 ROS 域 20 查找两台设备发布的图像话题和类型。 |
| EgoHalo + 双单目 UVC | EgoHalo 加自动识别的两台 UQ2168，或已绑定的两台 UVC |

Insight1 使用相机端已有的左右 namespace。两台设备 namespace 重复或没有可用图像发布者时，系统等待。Insight1 和普通 UVC 腕部使用图像采集配置，保留 EgoHalo 头部定位。腕部 6DoF 需要相应设备的位姿接入和实机验证。时钟和图像预检仍适用。

## 手动指定 UVC 左右

普通 UVC 或需要固定左右时，在 Jetson 安装目录运行以下命令。先只插左腕，用第一条命令记下其 `port`，然后插上右腕。按实际输出替换示例端口。

```bash
python3 -m insight_capture.runtime.camera_profiles uvc-devices
python3 -m insight_capture.runtime.camera_profiles bind-uvc --left 1-2.1 --right 1-2.2
```

绑定保存在 `config/uvc-bindings.json`。有唯一 USB 序列号的相机可换口插回。无唯一序列号的相机需插回原 USB 口。视频节点号改变时，原左右预览和录制话题仍会恢复。拔掉一只腕部时，另一只继续采集。拔插期间缺失的帧无法补回，正在录制的 Take 仍按现有断流规则处理。

安装或升级启用主机上的 `insight-capture-camera-profiles.service`。可用 `python3 -m insight_capture.runtime.camera_profiles scan` 查当前匹配结果，用 `cat config/.device` 查已启用的配置。

开发机用源代码部署时，可在安装目录运行 `bash deploy/install_camera_profiles.sh --user`，由有 Docker 权限的部署用户运行开机服务。

## EgoHalo 与双 UQ2168

同时接入 EgoHalo 和两台 UQ2168（`1bcf:28c4`）时可自动选中 `jetson-nx-egohalo-uvc`。有效的手动绑定优先。手机和 Qt 默认显示 EgoHalo 四路总览，可单独打开各腕部画面。自动模式按 USB 口排序，显示「腕部 1」「腕部 2」。当前配置记录两路对应的 USB 口，录制话题沿用原名称。

UQ2168 原生采集器协商 `1920×1200 MJPEG @ 90 fps`。按常规方式在手机或 Qt 进入任务、启停录制。EgoHalo 保存头部原件，Jetson 保存两腕，采集结束后在录制库按共同编号回传并合包。打开合成的 Take 可生成四路头部加两路腕部的同步回放。

每只腕的 `/uvc_{left,right}/camera/` 下有以下数据。

- `image_raw/compressed` 为相机原始 JPEG。图像 Header 时间来自 V4L2 单调时钟和采集进程启动时测得的 UTC 偏移。
- `frame_timing` 依次保存 V4L2 帧序号、驱动单调时刻、收到图像时的 Jetson 单调时刻、图像 Header UTC，时间单位均为纳秒。
- `imu_raw` 前两个小端 `uint64` 是 USB 请求开始、结束的 Jetson 单调纳秒，后接 128 字节原始 IMU 响应。设备采样时刻和 USB 请求时刻要分别解释。
- `uvc_metadata` 沿用同一封装，两个数依次为缓冲序号和读取时刻，后接驱动送出的原始载荷。

合包要求两腕均有图像、时序和 IMU 数据，原始两端文件随 Take 保留。先用三设备同时工作的一条 60 秒诊断 Take 核对六路画面，再录实际任务长度，检查每腕的实收帧率、帧序号缺口、IMU 连续性和共同有效区间。曝光同步精度需单独测量。

在 Jetson 空闲时，用该条合包 Take 和左右相机各自的鱼眼标定做离线定位。后两个参数是左右的图像安装旋转，可省略，默认都是 0°。

```bash
./scripts/localize_egohalo_uq2168.sh /path/to/Take /path/to/new-report.json \
  /path/to/left/mono.calibration.json /path/to/right/mono.calibration.json 0 180
```

脚本在私有建图进程中用 Take 中的 EgoHalo 前置双目、CameraInfo、静态 TF 和同曝光 VIO 建图。两腕只查询这张地图。报告给出每腕有效定位数、几何内点、重投影误差及通过验证的光学坐标位姿。返回 0 表示采集审计通过、两腕均有有效定位且至少一组定位相距不超过 50 ms。返回 2 时可从报告查看头部地图、左右腕和时序的各自结果。
