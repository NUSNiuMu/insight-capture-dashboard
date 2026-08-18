# `scripts/` 目录约定

这里仅保留稳定命令、运维脚本和历史命令名的薄 facade。业务实现统一位于
`insight_capture/`，工程验证工具位于 `tools/`。

关键入口：

- `run_dashboard.sh`：启动 Compose 与 Firefox Kiosk。
- `run_voice_control.sh`：启动宿主机离线语音服务。
- `multi_camera_dashboard_web.py`：兼容的 Dashboard Python 入口。
- `check_bag.py`、`lerobot_dataset_export.py`、`umi_dataset_export.py`：兼容 CLI。
- `select_device.sh`、`reboot_cameras.sh`、`sync_camera_restart.py`：设备维护。
- `post_processing.py`、`camera_setup.py`：历史 import facade。

新增 Python 业务模块不能放在此目录。外部调用仍依赖旧文件名时，应保留一个
只负责 bootstrap、导入和 `main()` 的入口。

目录规则和依赖方向见 [项目结构](../docs/PROJECT_STRUCTURE.md)。
