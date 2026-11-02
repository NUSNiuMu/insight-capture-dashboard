# Insight 客户部署

系统使用本地 Qt 桌面和手机采集页面，后端由 Docker 运行。没有桌面网页版或浏览器 kiosk。

## 首次安装

收到三个压缩包：同版本主镜像、SuperGlue 依赖镜像、部署包。解压部署包进入目录，把两个镜像压缩包放在旁边：

```bash
./update.sh insight-dashboard-v2.1.0.tar.gz
./native_dashboard/run.sh
```

版本号以收到的文件为准。更新命令加载 Dashboard 和同版本 LooperVIO 镜像，首次安装自动加载旁边的 SuperGlue 镜像，种入配置并启动服务。Qt 程序与运行库已打包，客户机不需要 Qt 编译器或外网下载。

## 日常操作

- Qt：任务、预览、录制、回放、录制库、合包、评分、优化、导出和设置。
- 手机：与 Jetson 同网，访问 `http://<Jetson-IP>:8765/mobile`；页面底部可管理任务及手动合包。
- 启动后端：`./scripts/run_dashboard.sh`；查看日志：`docker compose logs -f`。
- 退出 Qt 或关闭手机，不停止后端录制。

EgoHalo 可先录制再校准；停止后两端分别保存，保存完成即可录下一条。采集结束后在 Qt 录制库或手机管理区执行回传合包，不会在每次停止时自动合包。

## 设备切换

```bash
./scripts/select_device.sh jetson-nx-egohalo
./scripts/select_device.sh jetson-nx
```

前者是 EgoHalo + 双 Insight3，后者是 Insight9 + 双 Insight3。命令替换相机和运行配置，自动启停对应服务。结束录制/保存后再切换，切换后重新校准。自定义地址或外参应保留到对应 `config/devices/<profile>/` 模板中，避免再次切换时被默认值覆盖。

## 录制盘

在 `.env` 设置 `INSIGHT_ROSBAG_HOST_DIR` 为录制目录，`INSIGHT_ROSBAG_REQUIRED_SOURCE` 为实际挂载的稳定设备标识。盘不可用时会明确提示备用存储，不要通过清空标识隐藏异常。

`config/`、`rosbags/`、`outputs/`、`runs/` 是现场数据，升级保留。请在删除录制或处理结果前单独确认其用途。

## 升级

结束录制和保存后，执行新版本主镜像的 `update.sh` 命令。普通升级只需主镜像压缩包；配置和数据保留，宿主机工具、Compose 与 Qt 产物同步刷新。更新后关闭并重新打开 Qt。

已加载且满足当前配置/接口要求的旧镜像可用 `./update.sh --rollback <版本>` 切换。回退不会降级宿主机启动工具。

`/healthz` 只证明服务可响应；相机输入查看 `/api/cameras`，定位状态查看 `/api/mapping`，录制状态查看 `/api/mobile/status`。服务健康不等于图像连续或定位精度通过。
