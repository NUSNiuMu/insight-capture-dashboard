# Insight 离线语音控制与可选 OpenClaw

语音是无屏数采背包的主交互。宿主机 `insight-voice-control.service` 独占声卡，
Dashboard 容器不挂载 `/dev/snd`。固定命令的完整链路为：

```text
麦克风 → Silero VAD → SenseVoice INT8 → 精确命令匹配
       → localhost Dashboard API → Piper 本地播报
```

这条链路不需要网络、OpenClaw、Gateway 或 Codex。OpenClaw 只在说出唤醒词“宸境”
后的非固定自然语言请求中使用；它未安装或不可用时，固定命令仍持续工作。

## 固定离线命令

- 开始任务叠杯子：进入当前唯一的“叠杯子” Task；若已经在该 Task 中，只播报当前进度，
  不重置 Session 或 Take 编号；
- 当前任务多少条：播报当前 Task、当前 Session 已录制/有效/作废条数，以及下一条编号；
- 结束当前任务：结束本批 Session；录制中拒绝执行，再次开始同一 Task 会新建 Session；
- 开始录制：先运行三相机、定位、存储、必要 topics 与数据新鲜度 Preflight；失败时
  拒绝开始并直接播报原因；
- 停止录制：等待原生 MCAP recorder 排空，并写入 Take quick QC；
- 开始校准：清空 Insight9 sparse map 与两路 Insight3 global localization 状态；
- 检查相机：运行可选的固定检测位检查，并写入最近 Take；未通过时按右手、左手、头部
  相机分别播报问题，并明确是重新归位、对准检测位方向小范围扫动还是需要重新校准；命令
  发出后最多等待 12 秒接收新闭环，闭环结果有效 30 秒；
- 系统状态：播报 Preflight、定位、存储与录制可用性；
- 本条作废：只把最近 Take 标为 `operator_valid=false`，绝不删除原始 bag；
- 设置检测位：供需要固定治具的 task profile 使用。

采集中持续检测 camera stale、frame loss、mapping/localization、存储 fallback/低空间和
recorder I/O。异常持续超过阈值后，Dashboard 写入当前 Take 的 `anomaly_timeline`，并由
语音服务轮询 `/api/voice/alerts` 主动播报；录制不会自动删除或丢弃原始数据。

## 音频设备

录音和 ALSA 播放默认使用 `auto`。服务通过 `arecord -L` / `aplay -L` 选择稳定的
`plughw:CARD=<name>,DEV=<n>`，优先匹配 `LOOPER_AUDIO_DEVICE_HINT`（默认 `E3`），不依赖
可能随重插变化的 card index。PulseAudio 播放同样会按该 hint 自动选择 USB sink，不使用
可能指向 Jetson 板载声卡的桌面默认输出。E3 固件上报的 PCM dB 范围不可靠，服务启动时
只对匹配的 E3 PulseAudio card 启用 `ignore_dB`，再把共享 sink 恢复到 50%；其他声卡不受
影响。可按设备覆盖：

```bash
export LOOPER_CAPTURE_DEVICE=plughw:CARD=E3,DEV=0
export LOOPER_PLAYBACK_DEVICE=plughw:CARD=E3,DEV=0
export LOOPER_PLAYBACK_BACKEND=alsa   # 默认 pulse
export LOOPER_PULSE_SINK=alsa_output.usb-...E3...analog-stereo
export LOOPER_PLAYBACK_VOLUME=50
```

## 本地模型与测试

运行资源位于 `~/.local/share/looper-voice/`，不进入 Git：

- `python/`：sherpa-onnx 与 Piper；
- `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/`；
- `silero_vad.onnx`；
- `zh_CN-huayan-medium.onnx` 与 JSON。

Piper 默认使用自然语速、较低的声学与时长随机性，并在每个完整句子之间插入 220 毫秒
静音，避免多个音频块直接拼接造成吞句或异常断句。现场可通过
`--piper-length-scale`、`--piper-noise-scale`、`--piper-noise-w-scale` 和
`--piper-sentence-silence-ms` 微调。

以下测试完全离线：

```bash
scripts/run_voice.sh --speak-text '数采语音播报正常。'
scripts/run_voice.sh --wake-only
scripts/run_voice.sh --transcribe-once
scripts/run_voice.sh --echo-once
```

## 安装与日志

发布部署包已包含语音入口、安装器和 systemd unit。`update.sh` 会在每次安装或升级时从
Dashboard 镜像同步最新宿主机语音代码；上述模型已准备好时会自动安装、启用或升级服务，
并迁移旧的 `looper-openclaw-voice.service`。模型后补时可手动执行：

```bash
deploy/install_voice_control_service.sh
systemctl --user status insight-voice-control.service
journalctl --user -u insight-voice-control.service -f
```

安装器不要求 OpenClaw 存在，也不会把 voice unit 依赖到 network-online 或
openclaw-gateway。旧的 OpenClaw 专用启动脚本和 unit 已移除；可选能力由同一个离线
语音服务中的 adapter 提供。

## 隐私边界

原始音频始终在本机处理。固定命令的转写文本也不会发送给外部服务。只有操作者主动说
“宸境”进入可选自然语言模式后，后续转写文本和回答所需的本机状态才会交给 OpenClaw；
启用该能力前应取得设备使用者授权。
