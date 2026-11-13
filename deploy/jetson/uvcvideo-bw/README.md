# Jetson Orin：4 路 USB 摄像头的 uvcvideo 带宽补丁

## 问题

DECXIN 摄像头（`0c45:64ab`，USB 2.0，MJPEG 1280×720@60）在 Jetson Orin 上只能同时打开 2 路，
第 3 路起内核报：

```
usb 1-2.2: Not enough bandwidth for altsetting 3
VIDIOC_STREAMON returned -1 (No space left on device)
```

同样 4 路摄像头接在 x86（Intel xHCI）上一直正常。

## 原因

- Orin 所有 USB 2.0 设备共用一条 480 Mbit/s 总线，等时传输上限约每微帧 6000 字节（48 MB/s）。
- 摄像头固件为 MJPEG 一律申请最大档 altsetting 3：每微帧 3×1020 = 3060 字节（24.5 MB/s），
  2 路就占满 6120 字节。
- 实测每路 MJPEG 实际码率只有 3.8–5.1 MB/s，4 路合计约 18 MB/s，总线完全够用，是摄像头"多报"了需求。
- Orin 的 USB 控制器（tegra-xusb，固件闭源）严格按申请值拒绝；Intel 控制器不严格拦，所以 x86 能用。
  这个检查在控制器里，内核参数改不了，只能让驱动少申请。

## 补丁

`max_payload.patch` 给 uvcvideo 加一个模块参数 `max_payload`：选 altsetting 前把摄像头申请的
带宽封顶。设为 1020 时每路用 altsetting 1（每微帧 1020 字节，8.2 MB/s），4 路合计 4080 字节，
控制器放行；8.2 MB/s 仍高于实测每路峰值平均 5.1 MB/s。参数为 0（默认）时行为与原版完全一致。

`src-5.15.199/` 是 kernel.org `v5.15.199` 的原版 uvc 驱动源码（对应 JetPack 6 / L4T r36.5 的
`5.15.199-tegra` 内核）。其他内核版本由脚本自动从 kernel.org 下载对应源码再打补丁。

## 使用

在 Jetson 上（需要内核头文件 `/lib/modules/$(uname -r)/build`、gcc、make，JetPack 默认都有）：

```bash
cd deploy/jetson/uvcvideo-bw
./uvcvideo-bw.sh build                                   # 编译，不需要 sudo
docker compose -f ../../aruco/compose.yml stop           # 采集容器占着摄像头时卸载不了 uvcvideo
./uvcvideo-bw.sh try                                     # 临时加载，重启即失效
docker compose -f ../../aruco/compose.yml start
# 确认 4 路都能出图后，装进系统，开机自动加载：
./uvcvideo-bw.sh install
./uvcvideo-bw.sh status                                  # 查看当前加载的是哪个模块、封顶多少
```

卸载：`./uvcvideo-bw.sh uninstall`，然后重启（或停采集容器后 `sudo modprobe -r uvcvideo && sudo modprobe uvcvideo`）。

生效时内核日志每路有一行：

```
uvcvideo 1-2.2:1.1: Capping requested bandwidth 3060 B to 1020 B (max_payload)
```

## 验证结果（Orin NX Super，4 路同时 1280×720 MJPG@60，各 600 帧）

| 摄像头 | 用时 | 帧数 | 不完整帧 | 平均每帧 |
|---|---|---|---|---|
| video0 | 10.40 s | 600 | 0 | 70 KB |
| video2 | 10.40 s | 600 | 0 | 68 KB |
| video4 | 10.41 s | 600 | 0 | 77 KB |
| video6 | 10.41 s | 600 | 0 | 85 KB |

内核无带宽错误。

## 注意

- **升级 JetPack / 内核后失效**：新内核不会加载旧模块，又只能开 2 路。升级后重新
  `./uvcvideo-bw.sh build && ./uvcvideo-bw.sh install`。
- **画面极复杂时可能丢帧**：每路容量 8.2 MB/s，单帧突然变大超出时会丢帧。新场景先对着纹理复杂的画面测一段。
- **影响本机所有 UVC 摄像头**：参数是全局的。
- 模块未签名，加载时内核日志会提示 tainted，不影响使用（内核未开 `MODULE_SIG_FORCE`）。
- 彻底的硬件方案是加 PCIe/M.2 USB 扩展卡（瑞萨 uPD720201/720202、VIA VL805），把摄像头分到第二条总线上。
