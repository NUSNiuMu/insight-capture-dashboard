# 启动与相机校时

Jetson 开机不再自动重启全部相机，也不在等待相机后重启 Dashboard。
热点、DHCP、Web 服务各自启动；USB 设备发现与 DDS 输入按实际接口变化恢复。
本次 Jetson boot 的恢复检查点就绪前禁止开始录制：相机系统时钟收敛后，如图像/VIO
缺失或源时间基准错误，只重启该相机的采集服务。正常输入直接通过，不重启相机 OS。
检查点保存在 `/run/insight-capture-network/startup.json`，发现服务重启不会重复操作。
`host_setup.sh` 和客户升级使用的 `install_camera_discovery.sh` 会停用并移除旧
`insight-camera-reboot.service`。`scripts/reboot_cameras.sh` 只保留为人工维护工具。

手机“设备”页按相机显示等待 USB、正在校时、等待图像、等待 VIO、已就绪。
页面能打开、系统启动完成、相机输出、录制预检通过是不同阶段。

## Ubuntu 相机安装

`deploy/camera-clock/` 是本项目拥有的 Ubuntu 22.04 ARM64 相机适配包，含 chrony
二进制包、对应源码和许可证，不依赖另一个项目或在线 apt。确认采集空闲后，
停止相机采集服务，将这个目录复制到相机并以 root 执行 `bash install.sh`，随后启动采集服务。更换相机也需安装此适配，
同时配置 namespace、ROS domain 和 DDS 直连；不能把“支持任意有效 IP”
理解为无需配置任意出厂相机。

- 安装时同步修正 USB 启动命令和改 IP 生成器的掩码，分别沿用当前网络配置及生成器声明的前缀，避免改 IP 后与 DHCP 子网不一致。地址仍由设备配置决定。
- 每台相机只有一个 `insight-capture-clock.service`，替代原生一次性 ntpdate 和旧测试 chrony。
- NTP 源读取当前 `/etc/dnsmasq.conf` 的实际 Jetson 对端，地址变化只在同一 chronyd 内
  删除旧源、添加新源及更新访问控制，不重启 daemon、不触发新一轮 step。
- 采集进程未运行时，串行的 `sync-idle.sh` 丢弃旧 NTP 回归样本、重新测量，校正系统时间并连续 5 次确认 correction 达标；所有
  `S99all_run` 启动都经过这一门控。原生开机/API 的校时脚本也使用同一入口。
- 已存在 `insight_full` 时，该脚本只能等待测量，不能 step。常驻 daemon 从不执行
  makestep，运行期间持续校正，并将最大 slew 限为 100 ppm，避免大幅改变硬件时间基准。
- `wait-sync.sh` 以实际 system correction 小于 5 ms 放行，超时不启动采集；不会
  仅凭 NTP 服务的估计时间认定相机系统时间已收敛。
- 采集服务以 Wants 关联时钟，避免运行中维护时钟服务导致输入被 systemd 连带停止。
- Jetson 在获得物理 CDC-NCM 接口的有效 DHCP 租约后即允许该对端 NTP，不等待相机
  HTTP API，避免启动互相等待。HTTP 核验仍用于角色确认与应用使用，Wi-Fi/AP 无此授权。

修改地址需保持各链路网段不冲突。改 IP 或设备重启后录制前检查会重新测量，不沿用
旧地址的通过结果。仅 IP 变化不改变角色；相机/VIO 重启或换机后须重新确认公共地图
校准。当前未实现基于实体序列号的完整自动换机校准。

NTP 服务可能在系统时钟仍在收敛时返回已校正的估计时间。因此三台相机统一以 UDP
NTP 实测 + 相机实际系统 correction 就绪检查验收，并在保守误差上界额外计入 5 ms。
仍使用单机 10 ms、两机 20 ms 原门槛。录制开始/结束均保存测量结果。

## 验证边界

NTP 偏差、图像时间基准、公共地图定位和实际曝光同步分别验收。
预检不降低原有误差门槛来缩短启动等待。Jetson 以本地时钟为离线参考，绝对 UTC 和
RTC 跨断电保持能力仍需独立验证。物理断电测试与手机实际连接 AP 不能用软件重启、
电脑 USB 浏览器代替。

chrony 行为依据：[配置手册](https://chrony-project.org/doc/4.2/chrony.conf.html)
与 [chronyc 手册](https://chrony-project.org/doc/4.2/chronyc.html)。
