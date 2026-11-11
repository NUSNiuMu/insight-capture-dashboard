# 手机总览与离线采集

手机连接 Jetson 的采集热点，打开 `http://10.77.0.1:8765/mobile`。预览采用三路布局：Insight9 中间彩色相机位于左侧，两台腕部相机各取左目，右侧上方为左腕、下方为右腕。点击画面可放大单路，关闭后回到总览。

预览只在主动打开时工作。准备、录制、保存期间，服务端关闭预览进程、编码器和 WebRTC；完成后由仍可见的手机页面恢复。手机断网、锁屏或关闭页面不停止录制。状态查询不启动预览。相机画面超过 500 ms 未更新时对应区域变黑。

## 独立来源

页面组件、样式、浏览器 WebRTC 信令和 go2rtc 二进制于 2026-11-11 从 ego_dashboard `0235728` 一次性复制，随后在本项目独立适配。这个编号只记录来源，不是依赖、更新源或运行路径。本项目构建、运行、发布和升级不读取该项目任何目录、服务、配置或状态。没有软链接、共享源码、子模块或同步脚本。

- 页面代码：`web_dashboard/src/preview/`，构建产物及 Vue 运行库一起打包到 `web_dashboard/dist/static/preview.js`。
- WebRTC 服务：`deploy/bin/go2rtc`，许可证：`deploy/bin/LICENSE.go2rtc`。
- Jetson 适配：`insight_capture/media/jetson_preview.py`；使用 NVJPEG、nvvidconv 和 nvv4l2h264enc。灰度输入先转换为 I420；合成使用独立 NV12 内存画布。没有 X5 的硬件调用。
- `npm ci --prefix web_dashboard && node web_dashboard/build.js` 构建只使用本项目的 package/lock 文件。设备运行和升级直接使用随包的产物，不下载网页资源、编解码器、STUN/TURN 配置或模型。

## 参数含义

总览输出 1280×1280，区域为 736×1280、544×640、544×640；各相机等比例适配并保留黑边。30 fps，GOP 10，无 B 帧，关键帧带 SPS/PPS。总览使用固定 QP 23（Jetson `ratecontrol-enable=false`、`preset-level=0`、I/P QP=23）；单路使用 CBR 2.5 Mbit/s。固定 QP 的实际码率随场景变化。

总览以中间相机的源时间戳寻找距离不超过 25 ms 的腕部帧，最多等待 50 ms；短暂缺帧保留上一帧，500 ms 后置黑。这是预览配对策略，不是曝光同步保证。`/healthz` 的 worker 诊断保留配对计数、时间差、过期区域和编码错误。

## 离线网络与时间

`sudo deploy/configure_capture_network.sh` 升级已安装驱动的热点，不替换驱动。它保留 SSID/密码，关闭旧热点管理服务，安装独立 DHCP、本地域名解析和禁止转发规则。只向手机提供采集网段，不向办公室网络或相机 USB 网段转发。USB 调试 `192.168.55.1` 独立保留。

WebRTC 媒体与页面使用同一条直连链路。服务端从 HTTP socket 取得本机和客户端地址，为该本机地址绑定媒体端口，并限制本次信令候选；因此从 AP 打开的页面不会绕到办公室 Wi-Fi，USB 调试预览也不会串网。不同入口的轻量媒体转发共用每个视角的一份编码，新连接从带参数集的关键帧开始。没有固定的相机或客户端 IP 列表，也不需要公网 ICE 服务。

单无线电的 STA/AP 必须同信道：STA 已连接时 AP 跟随其信道；STA 未连接时 AP 自行启动，默认 5 GHz 信道 36。最终信道须在部署地区和手机上实际验证。运行脚本持续检查接口及信道，开机不依赖外网连接。

Jetson chrony 仅向当前发现的相机 USB 对端提供 NTP，使用本机时钟作为参考，启动时由 RTC 提供日期和时间。配置完全由本项目提供，不加载公网 NTP、DHCP 时间源或系统额外 sources；重新连上调试 Wi-Fi 也不会跳变采集时间。绝对 UTC 的校准只在空闲维护时进行，校准后需重启相机采集进程，再通过 NTP 和图像时间基准检查。RTC 是否能跨实际断电保持正确时间须单独验收。相机时钟相对偏差、图像时间戳配对、绝对 UTC 正确性是三个不同指标。NTP 检查仅测量，不在录制中调整系统时钟。

`jetson-nx` 配置启用录制前的时钟检查，单设备偏差界限 10 ms、两设备合计界限 20 ms，包含网络往返带来的不确定度。未通过时保留预览并阻止开始录制。录制清单保存开始和结束的测量报告。同步标志本身不作为精度证明。

## 验收

需覆盖真实手机 H.264 解码、总览及单路切换、后台关闭/恢复、准备/录制/保存的资源释放与恢复、离线 AP、自启动、NTP 偏差及图像时间戳、USB 调试保活。软件重启不等同于物理断电测试；验收记录应分别列出。

现场已经完成的检查和未验收边界见 [2026-11-11 验收记录](MOBILE_PREVIEW_ACCEPTANCE_20261111.md)。

Insight9 当前固件只提供 NTP 客户端，检查使用经严格主机密钥验证的 SSH 执行 `ntpdate -q`；不会启动第二套校时守护进程。需要在 Jetson 的专用 SSH 密钥和相机公钥授权完成后使用，密码不写入源码或配置。该命令的 delay 含实现保守量，因此头部单设备界限为 20 ms、往返上限 35 ms；仍受所有设备之间合计界限 20 ms 约束。具体是否达标以现场测量为准。

另有独立的图像时间基准检查，检测相机虽然已 NTP 同步、但采集进程仍使用旧启动偏移的情况。此检查比较图像 header 与 Jetson 收到图像的时间，只用于发现错误日期/时间基准，不能用于声称毫秒级同步精度。

设备接入时，在空闲状态执行 `deploy/guard_camera_time_sync.sh`，为相机固件已有的 `time_sync.sh` 加单实例锁。两个并发 ntpdate 都基于调整前的时钟测量，会导致大偏差被应用两次；锁避免这种启动竞争。脚本保留原文件备份，不新增校时服务。首次纠正大时间偏差需先停止相机采集服务，校时后再启动，并重新核验 ROS 图像 header。

DDS 数据面动态发现当前相机 USB 链路，仅发布这些链路和 loopback 的地址，不向相机公布办公室 Wi-Fi、手机热点、电脑 USB 调试或 Docker 网桥地址。这样 Wi-Fi/AP 切换不会改变采集数据面的可达地址。


## 改 IP 与更换设备

`jetson-nx` 使用 `ros_transport.camera_usb_only=true` 和 `clock_sync.discover_usb=true`，配置中不保存相机 IP 白名单。USB 网卡按 CDC-NCM 驱动发现；Jetson 端地址来自实际接口，相机地址来自该接口的 DHCP server identifier，再通过相机 `/api/ip-config` 核对双方地址。角色按 `/api/camera-config` 返回的 namespace 匹配，不按 IP、MAC、插口或插入顺序分配。

因此同一台相机改成 `169.254.50.1 ↔ 169.254.50.2` 或其他有效 IPv4 配对，不需要修改 InsightCapture 配置；也不要求末位必须为 `.1/.2`。多台相机的网段不能相互冲突，也不能与 AP、调试或其他连接的网段冲突。无匹配角色、重复 namespace、租约不符或发现结果过期时，时钟预检不会沿用旧设备的通过结果。

`insight-capture-camera-discovery.service` 维护自己的 `/run/insight-capture-network/cameras.json`，并动态授权/撤销 chrony 对端。服务开机启动；没有相机时 chrony 默认拒绝客户端。应用与在线校准的相机输入 participant 在 USB 网卡或地址变化后重建，运行中的任务和 Web 服务保留。Fast DDS 的进程内传输默认值与后续录制子进程配置同步更新；录制话题查询不再复用可能持有旧地址的 ROS CLI daemon。

首次接入一台相机，除配置正确的角色 namespace、ROS domain 和原生校时外，还需在相机空闲时运行本项目 `deploy/guard_camera_dds.sh`，随后重启其 `S99all_run`。它只启用 CycloneDDS 的 `DontRoute=true`，让相机从 Jetson 公布的地址中选择直连链路，避免改 IP 后丢失默认路由时错误地向另一条 USB 网段回传。该设置没有具体 IP，原生改 IP 接口会保留；三台现场相机均已配置。`deploy/guard_camera_time_sync.sh` 仍用于串行化相机原生 NTP 调用。

Insight9 的只读 SSH 时钟查询使用 `deploy/camera_clock_query.sh` 作为专用公钥的强制命令，目标从本次 `SSH_CONNECTION` 的 Jetson 地址取得。Jetson 使用角色对应的 `HostKeyAlias=insight-clock-<namespace>` 保存已验证的相机主机公钥，因此同一台设备改 IP 不会失去信任；换成另一台实体设备，需要首次登记新主机公钥和授权查询公钥。不会关闭严格主机密钥检查，也不把相机密码写入配置。

新设备接入后的在线校准仍由本项目自己的 LooperVIO 建图/重定位完成。IP 不是校准身份；仅改地址不会改变逻辑角色，但相机采集进程重启、VIO 重置和实际更换/移动相机后，需要重新确认校准结果。当前实现并未增加基于实体序列号的完整自动换机校准流程。

安装/升级独立发现服务使用 `sudo bash deploy/install_camera_discovery.sh`；已有采集热点的客户升级入口会更新该服务。客户镜像构建会编译本项目的 Fast DDS 适配库。源码挂载开发部署还需执行 `docker compose run --rm --no-deps insight-dashboard bash deploy/build_dds_network.sh`，将同一份库生成到本项目 `.native-build/`，不访问任何其他项目。
