# Jetson 单网卡采集热点

适用于已验证的 Jetson Orin NX、JetPack 6.2 / L4T R36.4.3、RTL8822CE 网卡。Jetson 保持连接上网 Wi-Fi，同时用同一网卡发采集热点；手机通过热点访问采集页面和互联网。

手机连接 `JETSON_CAPTURE_3189C2` 后，打开 **http://10.77.0.1:8765/mobile**。密码使用安装时设置的密码。Qt 无需启动。

## 安装到另一台同配置 Jetson

先让 Jetson 连接上网 Wi-Fi，再执行：

```bash
bash deploy/build_hotspot_driver.sh ~/insight-hotspot-build
sudo bash deploy/install_capture_hotspot.sh \
  ~/insight-hotspot-build/driver-source/nvidia-oot/drivers/net/wireless/realtek/rtl8822ce/rtl8822ce.ko \
  wlP1p1s0 JETSON_CAPTURE
sudo reboot
```

安装时输入热点密码。`wlP1p1s0` 应替换为 `nmcli device` 显示的上网无线接口。构建需要当前内核头文件、gcc、make、curl、tar，下载约 216 MB 官方源码。升级 JetPack 或内核后应重新构建驱动。

## 工作方式

原厂驱动只启用一个接口。同版本 NVIDIA 源码开启 `CONFIG_CONCURRENT_MODE` 和两个接口后，`wlP1p1s0` 用于上网，`capture_ap` 用于热点。两者共用无线信道和带宽。热点在上网 Wi-Fi 连接后启动，并按上网接口当前信道配置；更换上网 Wi-Fi 后重新启动热点。

NetworkManager 使用 `ipv4.method=shared` 提供 DHCP、DNS、IPv4 转发和 NAT。当前 Jetson 固定使用 iptables 后端，避免其内核不支持默认 nftables 共享规则的问题。上网 Wi-Fi 断开时不能继续共享互联网。

查看状态：

```bash
nmcli device
systemctl status insight-capture-hotspot
journalctl -u insight-capture-hotspot -b
```

停止热点：`sudo nmcli connection down jetson-capture-hotspot`。再次启动：`sudo systemctl restart insight-capture-hotspot`。

原驱动保存在 `/var/lib/insight-capture-hotspot/<内核版本>/rtl8822ce.stock.ko`；运行时没有自动切换到其他驱动或另一种热点方案。

## 本机验证

已验证双接口同时连接、手机通过 DHCP 获得 `10.77.0.x` 地址、访问 Jetson、域名解析和外网访问；Android 将热点判定为已验证的互联网连接。断开上网 Wi-Fi 后重新连接，热点和共享规则自动恢复，全程约 9 秒。手机可能切换到其他已保存网络，此时需重新选择采集热点。重启后的自动恢复仍需单独复核，不能用当前运行结果替代重启验证。
