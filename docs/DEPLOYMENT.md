# 部署与升级

系统只维护 Qt 桌面、手机页面和共用后端。Qt 在 Jetson 宿主机运行；Docker 运行相机适配、录制、建图定位和处理服务，不运行桌面浏览器。

## 当前设备与入口

```bash
./scripts/select_device.sh jetson-nx-egohalo  # 切换配置并同步服务
./scripts/run_dashboard.sh                   # 后端启动后返回
./native_dashboard/run.sh                    # Qt 桌面
```

`jetson-nx` 使用 Insight9 + 双 Insight3，`jetson-nx-egohalo` 使用 EgoHalo + 双 Insight3。设备差异位于 `config/devices/<name>/`，代码共用 `main`。

选择配置会覆盖现场 `cameras.json`、`runtime.json`，停止旧配置读取者，再按实际设置启停服务。跨 ROS 域启用转发，校正配置启用校正，编码输入启用 H.264 编码。无需手动指定 Compose profile。切换前结束录制和保存，切换后重新校准。

手机访问 `http://<Jetson-IP>:8765/mobile`。任务和两端合包在手机底部管理区或 Qt 中完成。退出 Qt、关闭手机页面或结束启动脚本，不停止 Docker 后端。

## 开发机准备

在 Orin NX / JetPack 6 的宿主机准备 Docker、NVIDIA 容器运行时和 Jetson Multimedia API 头文件。当前 Qt 与原生硬解为 aarch64 Jetson 实现，不承诺普通 PC 或其他 Jetson 型号直接兼容。

```bash
./native_dashboard/build.sh --prepare-builder
./native_dashboard/build.sh
./scripts/select_device.sh jetson-nx-egohalo --no-start
node web_dashboard/build.js
./deploy/setup_host.sh --device jetson-nx-egohalo
```

准备构建镜像和模型需要网络。已有依赖、Qt 程序及模型的客户机运行不需要公网，仍需相机局域网和本机后端。`--no-start` 仅供离线准备配置，不会检查或停止已运行服务。

## 生成发布包

在验证过的干净主线工作目录执行：

```bash
./deploy/build_release.sh v2.1.1
```

该命令构建 Qt、后端运行镜像、LooperVIO 核心和 SuperGlue 依赖，统一生成 **`release/insight-dashboard-v2.1.1.run`**。客户只接收这一个文件，包含全部运行镜像、更新器和 Qt 产物，无需联网下载依赖。

支持从 `jetson-nx` 或 `jetson-nx-egohalo` 配置打包。Qt 构建工具、头文件和构建缓存不交付；运行镜像仅带手机构建产物，不带桌面网页或浏览器。

## 首次安装与升级

停止录制、等待保存完成，关闭 Qt，然后执行：

```bash
bash insight-dashboard-v2.1.1.run /原安装目录
```

`/原安装目录` 是原来存放 `docker-compose.yml` 和 `config/` 的目录。首次安装指定目标目录即可。文件自动解包、加载全部镜像、迁移配置并启动服务；不需要旧版更新脚本。成功后清理未被容器使用的旧正式版本镜像，以及安装目录、其 `release/` 和当前升级文件所在目录中的旧发布包；当前版本与配置/标定/录制数据保留，清理记录在 `.upgrade-cleanup.json`。升级失败时不清理，不再提供旧镜像回退命令。完成后重新打开 Qt，手机网址不变。

v2.0.6、v2.0.7、v2.0.8 的实际配置已验证迁移。升级不按旧版本号拒绝，保留 `.env`、相机标定、任务和原始录制；修改前的配置备份到 `config/.upgrade-backup-*`。升级会停用并删除旧语音服务；推理初始化期间后端先启动。镜像损坏或后端无法启动时报告具体错误。

源码开发使用根目录 Compose，挂载源码；客户部署使用部署包 Compose，只挂载配置和数据。依赖改变必须重建镜像；只有源码挂载改变时，重启对应服务即可。

## 存储

在部署目录 `.env` 指定采集盘，例如：

```text
INSIGHT_ROSBAG_HOST_DIR=/media/nvidia/INSIGHT_USB/rosbags
INSIGHT_ROSBAG_REQUIRED_SOURCE=/dev/disk/by-uuid/<采集盘UUID>
```

核对实际挂载和稳定设备标识后启动。采集盘不可用时共用入口仍保留可见备用存储提示，不以隐藏设置使状态变绿。`config/`、`rosbags/`、`outputs/`、`runs/` 均为现场数据，不随源码清理删除。

## 验证

v2.1.1 固定为后续版本的升级基线，沿用单文件 `.run`、统一配置迁移和升级后清理流程。普通功能更新不重复 v2.0.6、v2.0.7、v2.0.8、v2.1.0 的全量升级矩阵；复用本次兼容性结果，只做构建产物校验、“上一正式版 → 新版”的基本升级检查，以及当次修改功能的针对性验证。

2026-11-04 已实际完成上述四版到 v2.1.1 的直接升级，覆盖 Insight9 和 EgoHalo 配置；配置、标定、任务和原始 MCAP 保留，升级后的 Qt 与手机页面检查通过。

升级器、配置迁移、安装布局、Compose、运行依赖或录制数据格式发生变化时，再针对受影响的旧版本补测，不自动重跑全部历史版本。

- `/healthz`：后端可响应。
- `/api/cameras`：对应相机图像/VIO是否新鲜。
- `/api/mapping`：地图、三路定位与时钟状态。
- `/api/mobile/status`：实际录制、保存和设备列表。
- Qt：六个工作页面及空间视图、回放及手动合包。
- 手机：任务、录制启停、校准、单路预览和合包。

健康状态不代替相机有数据、录制不掉帧或定位精度验收。开发验收记录要区分模拟 UI、真实设备、保存文件和离线测量。

`deploy/systemd/insight-capture.service` 是启动模板，调用与手动入口相同的服务选择逻辑。部署时按实际用户和目录配置该模板；已有 Docker 服务仍保留各自重启策略，不在系统里新增第二套定位进程。
