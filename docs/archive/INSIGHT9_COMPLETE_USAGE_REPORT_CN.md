# Insight9 使用文档整理记录（2026-11-18）

日常操作请从 [使用手册](../USAGE.md) 进入。此页保留 2026-11-18 的文档整理记录。

适用设备：Insight9 头部相机 + Insight3 A 右腕相机 + Insight3 B 左腕相机，配置为 `jetson-nx`。

| 你要做什么 | 阅读哪份文档 |
| --- | --- |
| 用手机连接设备、选择任务、录制和保存 | [手机采集指南](../INSIGHT9_PHONE_GUIDE_CN.md) |
| 用 Qt 查看轨迹、管理任务、回放、检查数据和优化 | [电脑 Qt 使用指南](../INSIGHT9_QT_GUIDE_CN.md) |
| 查看项目框架、配置、版本、启动、升级和数据备份 | [配置与维护说明](../INSIGHT9_MAINTENANCE_CN.md) |

手机连接 **Jetson 提供的 Wi-Fi 热点**，浏览器入口为 `http://10.77.0.1`。电脑篇所说的 Qt 是运行在 Jetson 上的原生桌面程序。

手机与 Qt 控制同一套采集服务，任务、录制和地图状态共享。一次采集由一名操作者负责起停，另一端查看即可。

- [手机采集指南 PDF](../../output/pdf/Insight9_手机采集指南.pdf)
- [电脑 Qt 使用指南 PDF](../../output/pdf/Insight9_电脑Qt使用指南.pdf)

核对日期：2026-11-18。对应远端主线：`main / 5bd32c0bfa6d7b84d71d4b657e9996aa54f0c7e6`。安装与验证范围见配置与维护说明。
