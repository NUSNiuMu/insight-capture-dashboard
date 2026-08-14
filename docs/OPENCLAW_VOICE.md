# Looper OpenClaw 语音助手

Looper 在 Jetson 宿主机上提供“唤醒词 → 自然语言 → 数采工具 → 语音回复”的本地入口。
当前 USB 声卡使用 ALSA 标识 `plughw:E3,0`，同时提供麦克风输入和音响输出。

## 工作链路

1. Vosk 英文小模型持续离线监听唤醒词 `Looper`，Silero VAD 检测到 0.5 秒静音后确认唤醒；
2. 立即播放服务启动时预生成的“我在”，播完后重新打开麦克风；
3. Silero VAD 截取用户随后说的一段话，SenseVoice INT8 在本机离线转写中文；
4. 只有转写文本发送给本机 OpenClaw，OpenClaw 使用当前用户的 Codex 登录完成推理；
5. `insight_capture` 插件仅通过 `http://127.0.0.1:8765` 访问 Dashboard；
6. 简短回复由服务启动时常驻加载的本地 Piper 合成，并通过同一 USB 音响播放。

原始音频不会发送给 OpenClaw、Codex 或 Dashboard。唤醒后的转写文本以及回答请求所需
的本机数采状态会交给当前 Codex 订阅处理，因此启用常驻服务前必须获得设备使用者明确
授权。OpenClaw Gateway HTTP 对话端点保持关闭；语音桥直接使用本地 CLI，不开放网络
监听端口。

## 本机资源

运行资源位于 `~/.local/share/looper-voice/`，不进入 Git：

- `python/`：Vosk 0.3.45、sherpa-onnx 1.13.5、Piper 1.6.0；
- `vosk-model-small-en-us-0.15/`：Looper 唤醒；
- `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/`：中文转写；
- `silero_vad.onnx`：语音起止检测；
- `zh_CN-huayan-medium.onnx` 与 JSON：中文播报。

SenseVoice 模型与麦克风用法来自 sherpa-onnx 官方文档：
<https://k2-fsa.github.io/sherpa/onnx/sense-voice/python-api.html>。

## 离线验证

以下测试都不会向 OpenClaw 或外部服务发送内容：

```bash
# 只测试音响
scripts/run_openclaw_voice.sh --speak-text 'Looper 语音播报正常。'

# 只测试唤醒词
scripts/run_openclaw_voice.sh --wake-only

# 只转写下一句话
scripts/run_openclaw_voice.sh --transcribe-once

# 唤醒、转写、原样播报一次
scripts/run_openclaw_voice.sh --echo-once
```

正常交互是单独说“Looper”并停顿 0.5 秒，听到“我在”后再说命令。唤醒音频不会混入
命令转写。默认一次命令等待 15 秒，每次唤醒只处理一句话；回答后必须重新说唤醒词，
避免把周围谈话误当成连续指令。

服务每次启动都会把 USB 音响的 PCM 音量恢复到 30%。可通过环境变量
`LOOPER_PLAYBACK_VOLUME` 覆盖。

语音入口固定使用 `openai/gpt-5.6-luna`、`thinking=off` 和 OpenClaw `fastMode=true`，
并禁用默认 agent 的无关技能提示，避免复杂编码模型与额外上下文拖慢短语音问答。
安装脚本会幂等写入这些 OpenClaw 配置。

## OpenClaw 与数采工具

插件目录是 `integrations/openclaw-insight-capture/`，工作区是
`integrations/openclaw-workspace/`。权限分为只读的 `insight_capture_status` 和写操作
`insight_capture_recording`；后者必须由设备使用者单独授权，并且每次开始/停止仍要求
当前语音明确下令。开始录制使用服务器默认 topics，并生成
`looper_record_YYYYmmdd_HHMMSS`；停止接口只接受这个前缀。若网页、手势或其他程序正在
录制，Looper 会拒绝覆盖或停止。

当前相机未发布任何可录 topic 时，状态查询仍可用，但不要测试开始录制。先在 Recording
页确认 `default_selected_topics` 非空。

## 常驻服务

在设备使用者明确同意发送唤醒后文本和必要数采状态后，再执行：

```bash
scripts/install_openclaw_voice_service.sh
systemctl --user status looper-openclaw-voice.service
```

服务日志：

```bash
journalctl --user -u looper-openclaw-voice.service -f
```

停止并禁止自启动：

```bash
systemctl --user disable --now looper-openclaw-voice.service
```

不要同时重新启用 Settings 中的旧 `voice_recording` worker；两个进程会争用同一个 USB
麦克风。
