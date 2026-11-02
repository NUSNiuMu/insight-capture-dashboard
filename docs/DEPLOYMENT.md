# 部署与升级

系统只维护 Qt 桌面、手机页面和共用后端。Qt 在 Jetson 宿主机运行；Docker 运行相机适配、录制、建图定位和处理服务，不运行桌面浏览器。

## 当前设备与入口

```bash
./scripts/select_device.sh jetson-nx-egohalo  # 切换配置并同步服务
./scripts/run_dashboard.sh                   # 后端启动后返回
./native_dashboard/run.sh                    # Qt 桌面
```

`jetson-nx` 使用 Insight9 + 双 Insight3，`jetson-nx-egohalo` 使用 EgoHalo + 双 Insight3；`lite` 与 `lite-779` 是开发配置。设备差异位于 `config/devices/<name>/`，代码共用 `main`。

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
./deploy/build_release.sh v2.1.0
```

该命令构建 Qt、后端运行镜像、独立的 LooperVIO 核心和 SuperGlue 依赖，生成：

- `insight-dashboard-v2.1.0.tar.gz`：包含同版本 Dashboard 与 LooperVIO 镜像。
- `insight-superglue-validation-25.04.tar.gz`：首次安装所需的推理镜像。
- `insight-dashboard-deploy-v2.1.0.tar.gz`：Compose、更新和启动脚本、Qt 可执行文件及运行库。

支持从 `jetson-nx` 或 `jetson-nx-egohalo` 配置打包。Qt 构建工具、头文件和构建缓存不交付；运行镜像仅带手机构建产物，不带桌面网页、浏览器或浏览器测试环境。原生建图运行镜像不携带 WiLoR、COLMAP、Python 或 ROS。

不要把镜像标称体积相加当作真实磁盘占用：旧版本可能共享层。删除依赖需要重新构建镜像，删除工作区文件或在运行容器中临时卸载不会缩小既有镜像层。

## 首次安装与升级

解压部署包，把镜像压缩包放到同一目录，在 Jetson 宿主机执行：

```bash
./update.sh insight-dashboard-v2.1.0.tar.gz
./native_dashboard/run.sh
```

第一次安装从镜像种入配置；之后升级保留现场配置、标定和数据，同时刷新 Compose、设备模板、宿主机工具和 Qt 产物。Qt 二进制及库通过新文件替换，不截断旧进程已映射的库；升级后关闭并重新打开 Qt。不得录制中升级。

基础推理镜像已加载时无需每次传输。普通升级只需同版本主镜像压缩包，Qt 更新包含在镜像里，无需客户安装编译器或下载 Qt。回退已加载旧版本时保留宿主机工具；旧镜像仍需满足该版本后端/原生核心与当前配置的兼容要求。

源码开发使用根目录 Compose，挂载源码；客户部署使用部署包 Compose，只挂载配置和数据。依赖改变必须重建镜像；只有源码挂载改变时，重启对应服务即可。

## 存储

在部署目录 `.env` 指定采集盘，例如：

```text
INSIGHT_ROSBAG_HOST_DIR=/media/nvidia/INSIGHT_USB/rosbags
INSIGHT_ROSBAG_REQUIRED_SOURCE=/dev/disk/by-uuid/<采集盘UUID>
```

核对实际挂载和稳定设备标识后启动。采集盘不可用时共用入口仍保留可见备用存储提示，不以隐藏设置使状态变绿。`config/`、`rosbags/`、`outputs/`、`runs/` 均为现场数据，不随源码清理删除。

## 验证

- `/healthz`：后端可响应。
- `/api/cameras`：对应相机图像/VIO是否新鲜。
- `/api/mapping`：地图、三路定位与时钟状态。
- `/api/mobile/status`：实际录制、保存和设备列表。
- Qt：九个页面、回放及手动合包。
- 手机：任务、录制启停、校准、单路预览和合包。

健康状态不代替相机有数据、录制不掉帧或定位精度验收。开发验收记录要区分模拟 UI、真实设备、保存文件和离线测量。

`deploy/systemd/insight-capture.service` 是启动模板，调用与手动入口相同的服务选择逻辑。部署时按实际用户和目录配置该模板；已有 Docker 服务仍保留各自重启策略，不在系统里新增第二套定位进程。
