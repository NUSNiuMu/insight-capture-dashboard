# 宸境 OpenClaw 语音助手

宸境在 Jetson 宿主机上提供“唤醒词 → 自然语言 → 数采工具 → 语音回复”的本地入口。
当前 USB 声卡使用 ALSA 标识 `plughw:E3,0`，同时提供麦克风输入和音响输出。

## 工作链路

1. Silero VAD 以 0.5 秒静音切分独立的中文短句；
2. SenseVoice INT8 在本机离线转写；整句固定录制/校准指令立即本地执行，无需唤醒；
3. 只有整句匹配“宸境”或配置的同音转写时才进入 OpenClaw 模式；
4. 立即播放服务启动时预生成的“我在”，播完后重新打开麦克风；
5. Silero VAD 截取随后的命令，由同一个 SenseVoice 实例离线转写并发送给 OpenClaw；
6. OpenClaw 使用当前用户的 Codex 登录完成推理；
7. `insight_capture` 插件仅通过 `http://127.0.0.1:8765` 访问 Dashboard；
8. 简短回复由服务启动时常驻加载的本地 Piper 合成，并经 PulseAudio 共享通道从同一 USB
   音响播放，避免与桌面播报程序互相独占声卡。

原始音频不会发送给 OpenClaw、Codex 或 Dashboard。唤醒后的转写文本以及回答请求所需
的本机数采状态会交给当前 Codex 订阅处理，因此启用常驻服务前必须获得设备使用者明确
授权。OpenClaw Gateway 仅绑定 `127.0.0.1` 并使用 token 鉴权，HTTP Chat Completions
端点保持关闭；语音桥通过官方 Gateway 客户端复用常驻进程，不开放局域网监听。

## 本机资源

运行资源位于 `~/.local/share/looper-voice/`，不进入 Git：

- `python/`：sherpa-onnx 1.13.5、Piper 1.6.0；
- `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/`：宸境唤醒与命令转写共用；
- `silero_vad.onnx`：语音起止检测；
- `zh_CN-huayan-medium.onnx` 与 JSON：中文播报。

SenseVoice 模型与麦克风用法来自 sherpa-onnx 官方文档：
<https://k2-fsa.github.io/sherpa/onnx/sense-voice/python-api.html>。

## 离线验证

以下测试都不会向 OpenClaw 或外部服务发送内容：

```bash
# 只测试音响
scripts/run_openclaw_voice.sh --speak-text '宸境语音播报正常。'

# 只测试唤醒词
scripts/run_openclaw_voice.sh --wake-only

# 只转写下一句话
scripts/run_openclaw_voice.sh --transcribe-once

# 唤醒、转写、原样播报一次
scripts/run_openclaw_voice.sh --echo-once
```

正常交互是单独说“宸境”并停顿 0.5 秒，听到“我在”后再说命令。唤醒音频不会混入
命令转写。默认一次命令等待 15 秒，每次唤醒只处理一句话；回答后必须重新说唤醒词，
避免把周围谈话误当成连续指令。

以下短指令常驻监听，直接说即可；它们使用精确匹配的本地快捷路径，执行和回复都不依赖
OpenClaw，也不需要先说“宸境”：

- “开始录制”“开始录像”“开始采集”；
- “结束录制”“停止录制”及对应的录像、采集说法；
- “开始校准”“重新校准”“重置校准”；
- “检查相机”“开始检测”“位置检测”；
- “设置检测位”“记录检测位”“保存检测位”（仅标定负责人使用）。

服务启动时会预生成开始处理、成功、重复录制和失败等确认语音，命中快捷指令后直接播放缓存。
固定“开始录制”和“停止录制”会立即分别播报“初始化录制中，请稍等”和“正在结束录制”，无需等待
OpenClaw 或 rosbag 的 DDS 订阅/排空；后台实际完成后再播报最终结果。所有语音录制入口采用
确认闭环：固定“开始录制”指令必须完整播放“录制已经开始”；
OpenClaw 自然语言工具调用则在请求前后核对录制状态，必须完整播放本轮回复。任一路径没有
成功反馈时，语音桥都会持续调用受限停止接口，直到本轮新建的 `looper_record_*` 被停止。
播放异常只记录为单次交互错误，不能再导致常驻监听服务退出，因此不允许出现无声音反馈却
继续后台录制。人工、网页或手势创建的其他录制不会被该回滚逻辑停止。
每次固定指令会向服务日志输出 `local_command_timing`；其中包含 VAD/识别参数、两段播报、
Dashboard HTTP 总耗时及后端返回的 `start_timings`。`resume_requested_offset_sec` 与
`resume_confirmed_offset_sec` 给出实际开始写入的时间上下界，不能用第一段处理中播报结束
作为录制起点。
“开始校准”调用 `/api/mapping/reset`，语义是清空 Insight9 地图和 Insight3 全局定位状态，
立即开始新一轮在线校准，不是夹爪尺寸标定。语音桥随后在后台检查本机 mapping 状态；
Insight3 A、B 在本次 reset 后首次同时进入 `localized=true` 时，播放一次预生成的
“校准完成”。

每个采集单元停止后，确认语音会要求把两台 Insight3 放回固定检测位，并让头戴 Insight9
短暂扫视已建图工作区。说“检查相机”后，系统先播报“开始检测”，再比较两路 Insight3
当前全局 Pose 与保存值，并要求 Insight9 相对冻结的自然特征地图产生一条新闭环，最后播报
通过、重新放置/扫视或重新校准。两路 Insight3 必须已完成本轮全局定位，但不要求检测瞬间
再次命中地图；Insight9 不要求回到固定头部 Pose。“设置检测位”会保存双手 Pose 并冻结
当前 Insight9 地图，覆盖原有检测位基准，只能在校准已人工确认正确时使用。完整现场流程见
[叠杯批量数据采集标准作业规范](CUP_STACKING_DATA_COLLECTION_SOP.md)。

“宸境”只用于打开 OpenClaw 对话。听到“我在”后说出的下一句话固定交给 OpenClaw，
即使内容恰好是“开始录制”，也不会再由本地快捷路径截获。

服务每次启动都会把 USB 音响的 PCM 音量恢复到 50%。可通过环境变量
`LOOPER_PLAYBACK_VOLUME` 覆盖。默认播放后端是 PulseAudio；仅在没有 PulseAudio 的设备上
显式设置 `LOOPER_PLAYBACK_BACKEND=alsa`。需要固定共享输出设备时可用
`LOOPER_PULSE_SINK` 指定 sink 名称。

语音入口固定使用 `openai/gpt-5.6-luna`、`thinking=off` 和 OpenClaw `fastMode=true`；
Fast 最终向 Codex/OpenAI 请求传递 Priority service tier，并禁用默认 agent 的无关技能
提示，避免复杂编码模型与额外上下文拖慢短语音问答。安装脚本会幂等写入这些 OpenClaw
配置，并安装、启动常驻 Gateway。

## OpenClaw 与数采工具

插件目录是 `integrations/openclaw-insight-capture/`，工作区是
`integrations/openclaw-workspace/`。权限分为只读的 `insight_capture_status` 和写操作
`insight_capture_recording`；后者必须由设备使用者单独授权。自然语言录制请求仍通过
OpenClaw 工具，固定短指令则使用语音桥的本地快捷路径；两者每次开始/停止都要求当前
语音明确下令。开始录制使用服务器默认 topics，并生成
`looper_record_YYYYmmdd_HHMMSS`；停止接口只接受这个前缀。若网页、手势或其他程序正在
录制，宸境会拒绝覆盖或停止。

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

Dashboard 不再包含另一套麦克风 worker；所有声控入口统一由该宿主机服务提供。
