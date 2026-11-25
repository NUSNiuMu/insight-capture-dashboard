# 相机组合自动识别与切换

Jetson 自动发现设备，连续两轮识别到唯一一套完整组合后才准备切换。每套需要一个头部设备和两个腕部设备。开始录制、录制中、保存、合包及回放期间等待，空闲时再次核对设备并应用配置。相同组合、相同地址重插时沿用当前配置，视频节点由采集服务重新打开。

| 组合 | 设备身份 |
| --- | --- |
| Insight9 + 双 Insight3 | `insight9_a`、`insight3_a`（右）、`insight3_b`（左） |
| EgoHalo + 双 Insight3 | EgoHalo 采集服务声明的型号，以及同样两个 Insight3 namespace |
| EgoHalo + 双 Insight1 | Insight1 固件型号接口，以及 `insight1_a`（右）、`insight1_b`（左）。Jetson 在 ROS 域 20 查找两台设备发布的图像话题和类型。 |
| EgoHalo + 双单目 UVC | EgoHalo 加已绑定的两台 UVC |

Insight1 使用相机端已有的左右 namespace。两台设备 namespace 重复或没有可用图像发布者时，系统等待。Insight1 和普通 UVC 腕部使用图像采集配置，保留 EgoHalo 头部定位。腕部 6DoF 需要相应设备的位姿接入和实机验证。时钟和图像预检仍适用。

## 双 UVC 首次绑定

在 Jetson 安装目录运行以下命令。先只插左腕，用第一条命令记下其 `port`，然后插上右腕。按实际输出替换示例端口。

```bash
python3 -m insight_capture.runtime.camera_profiles uvc-devices
python3 -m insight_capture.runtime.camera_profiles bind-uvc --left 1-2.1 --right 1-2.2
```

绑定保存在 `config/uvc-bindings.json`。有唯一 USB 序列号的相机可换口插回。无唯一序列号的相机需插回原 USB 口。视频节点号改变时，原左右预览和录制话题仍会恢复。拔掉一只腕部时，另一只继续采集。拔插期间缺失的帧无法补回，正在录制的 Take 仍按现有断流规则处理。

安装或升级启用主机上的 `insight-capture-camera-profiles.service`。可用 `python3 -m insight_capture.runtime.camera_profiles scan` 查当前匹配结果，用 `cat config/.device` 查已启用的配置。

开发机用源代码部署时，可在安装目录运行 `bash deploy/install_camera_profiles.sh --user`，由有 Docker 权限的部署用户运行开机服务。
