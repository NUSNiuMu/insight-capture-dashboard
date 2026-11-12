# Jetson 单网卡采集热点

适用于已验证的 Jetson Orin NX、JetPack 6.2 / L4T R36.4.3、RTL8822CE 网卡。Jetson 使用独立采集热点；手机通过热点访问采集页面，不经 Jetson 转发到办公室网络或互联网。办公室 Wi-Fi 可以同时连接，USB 网络保留为调试通道。

手机连接 `JETSON_CAPTURE_3189C2` 后，打开 **http://10.77.0.1:8765/mobile**。密码使用安装时设置的密码。Qt 无需启动。

## 安装到另一台同配置 Jetson

以下是需要下载驱动源码的开发安装流程。离线设备使用预先构建、随 InsightCapture 交付的驱动与资源；运行热点不要求办公室 Wi-Fi 已连接。

```bash
bash deploy/build_hotspot_driver.sh ~/insight-hotspot-build
sudo bash deploy/install_capture_hotspot.sh \
  ~/insight-hotspot-build/driver-source/nvidia-oot/drivers/net/wireless/realtek/rtl8822ce/rtl8822ce.ko \
  wlP1p1s0 JETSON_CAPTURE
sudo reboot
```

安装时输入热点密码。`wlP1p1s0` 应替换为 `nmcli device` 显示的上网无线接口。构建需要当前内核头文件、gcc、make、curl、tar，下载约 216 MB 官方源码。升级 JetPack 或内核后应重新构建驱动。

## 工作方式

原厂驱动只启用一个接口。同版本 NVIDIA 源码开启 `CONFIG_CONCURRENT_MODE` 和两个接口后，`wlP1p1s0` 用于办公室 Wi-Fi，`capture_ap` 用于热点。两者共用无线信道和带宽。有 STA 连接时 AP 跟随其信道；STA 断开后保留已经工作的 AP 信道，避免无必要地打断手机。无 STA 且需要新建热点时，采用配置的默认信道（默认 36）。STA 加入不同信道时仍可能需要切换 AP 信道，这是同一无线芯片的约束。

NetworkManager 使用 `ipv4.method=manual`，AP 地址为 `10.77.0.1/24`；自有 dnsmasq 服务提供 DHCP，隔离服务阻止 AP 的跨接口转发。手机可能显示“无互联网”，但采集页面、直连 WebRTC 和本地控制可离线使用。相机通过独立 USB 链路向 Jetson 校时，不依赖公网 NTP。

查看状态：

```bash
nmcli device
systemctl status insight-capture-hotspot
journalctl -u insight-capture-hotspot -b
```

停止热点时先停守护进程，再停连接，否则守护进程会重新拉起 AP：

```bash
sudo systemctl stop insight-capture-hotspot
sudo nmcli connection down jetson-capture-hotspot
```

再次启动：`sudo systemctl start insight-capture-hotspot`。

原驱动保存在 `/var/lib/insight-capture-hotspot/<内核版本>/rtl8822ce.stock.ko`；运行时没有自动切换到其他驱动或另一种热点方案。

## 本机验证

2026-11-13 真实 OnePlus / Android 11 / Chrome 132 验证：手机通过 DHCP 获得 `10.77.0.106`；控制与 WebRTC 媒体均走 AP。旧脚本在断开 STA 时把 AP 从 157 改回 36，手机因此转投办公室网络。`5a1fb79` 修复后，在 STA/AP 均为 52 的窗口重复断开 STA，AP 保持原信道，同一 RTP 流继续解码；移除所有互联网默认路由后，手机仍完成开始、停止、保存与预览恢复。

这是一次实机离线切换验证，不涵盖所有无线环境、iPhone 或物理冷启动。软件重启与改 IP 证据见 `MOBILE_EDGE_ACCEPTANCE_20261112.md`；本轮详细范围见 `CUSTOMER_ANDROID_ACCEPTANCE_20261113.md`。HTTP 文件下载在该手机 Chrome 中触发“无法安全地下载文件”，最终下载仍需用户处理浏览器确认后补验。
