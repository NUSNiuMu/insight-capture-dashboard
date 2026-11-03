# Insight 安装与升级

停止录制、等待保存完成，关闭 Qt，然后运行：

```bash
bash insight-dashboard-v2.1.0.run /原安装目录
```

`/原安装目录` 是原来存放 `docker-compose.yml` 和 `config/` 的目录。首次安装也使用这条命令，指定要安装的位置即可。

升级自动加载全部镜像、迁移旧配置并启动服务。原配置、标定和录制数据保留。

完成后重新打开 Qt，手机网址不变：`http://<Jetson-IP>:8765/mobile`。
