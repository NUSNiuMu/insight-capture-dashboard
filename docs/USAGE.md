# 使用手册

日常启动适用于 Insight9（`jetson-nx`）和 EgoHalo（`jetson-nx-egohalo`）两套采集配置。下表的 Insight9 专用指南对应 Insight9 头部、Insight3 A 右腕和 Insight3 B 左腕。

电脑端使用 Jetson 原生 Qt；手机端使用浏览器。电脑桌面网页和浏览器 kiosk 已移除。

文档核对日期：2026-11-18。对应源码：`main / 5bd32c0bfa6d7b84d71d4b657e9996aa54f0c7e6`，已与远端主线核对。现场安装版本以安装目录 `.env` 中的 `INSIGHT_VERSION` 和实际运行产物为准。

## 按操作端阅读

| 你要做什么 | 当前手册 | PDF |
| --- | --- | --- |
| 手机现场采集 | [手机采集指南](INSIGHT9_PHONE_GUIDE_CN.md) | [手机篇](../output/pdf/Insight9_手机采集指南.pdf) |
| Jetson 本机 Qt 操作与数据复查 | [电脑 Qt 使用指南](INSIGHT9_QT_GUIDE_CN.md) | [电脑篇](../output/pdf/Insight9_电脑Qt使用指南.pdf) |
| 配置、启动、升级、备份和版本核对 | [配置与维护说明](INSIGHT9_MAINTENANCE_CN.md) | |
| 连接与操作采集背包硬件 | | [采集背包硬件手册](CAPTURE_BACKPACK_HARDWARE_MANUAL.pdf) |

EgoHalo 配置的双端采集及合包见 [EgoHalo 双端录制](EGOHALO_DISTRIBUTED_RECORDING.md)。录制丢帧或源时间戳异常可从 [完整性检查与轨迹评分](INSIGHT9_QT_GUIDE_CN.md#5-检查完整性并查看轨迹评分) 查起。

## 当前启动与连接方式

2026-11-18 现场核对时，`nvidia@192.168.19.222` 上的 Insight9 项目目录为 **`/home/nvidia/insight-capture-dashboard`**，设备源码为 `011a065`。以下命令在实际项目或安装根目录执行。

**手机**：连接 Jetson 提供的 Wi-Fi 热点，浏览器打开 `http://10.77.0.1`。备用地址为 `http://10.77.0.1:8765/mobile`；其他可互通网络使用 `http://<Jetson-IP>:8765/mobile`。手机端无需执行启动命令。

**Qt**：在 Jetson 的项目或安装根目录执行：

```bash
./native_dashboard/run.sh
```

它只启动原生 Qt 桌面，默认连接 `http://127.0.0.1:8765`，30 FPS、全屏。客户机使用已安装的发布程序和运行库。源码开发时先执行 `./native_dashboard/build.sh`，启动脚本不会临时安装或编译依赖。

**后端**：四个服务均配置 `restart: unless-stopped`。已安装并启动过、Docker 开机运行且容器未被手动停止时，会随 Docker 恢复。开机先等待就绪；仅在维护人员确认需要启动、且没有录制或保存操作时，在同一根目录执行：

```bash
./scripts/run_dashboard.sh
```

该入口启动采集服务后返回，不打开桌面浏览器。后端已运行且空闲时，脚本可能重启后端，不用它查询状态。健康查询与只读日志见配置与维护说明。

手机和 Qt 共用任务、录制与地图状态。一次采集由一名操作者负责开始和停止，另一端可以查看状态。关闭页面或 Qt 后录制会继续，必须明确结束并等待保存完成。
