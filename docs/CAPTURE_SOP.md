# 无屏背包采集流程

## 上电与 Preflight

上电后说“系统状态”。系统检查三相机图像新鲜度、三路 pose、Insight9 mapping、两路
Insight3 localization、录制存储可写性和剩余空间，以及必要 recorder topics。只有结果
通过时“开始录制”才会启动原生 MCAP recorder；失败原因直接由音响播报。

## Session / Take

数据按三级组织：Task 表示动作类型，Session 表示一次连续采集批次，Take 表示该批次中的
一条录制。目前 Task 只有“叠杯子”，配置在 `config/capture_tasks.json`。当天服务重启会
恢复未结束的 Session；明确结束 Task 后再次开始，才会创建新 Session 并从 Take 1 计数。

现场可直接说“开始任务叠杯子”“当前任务多少条”“结束当前任务”。开始同一个进行中的
Task 不会清零计数；录制过程中不能切换或结束 Task。每次录制分配递增的 `take_id`，元数据
保存在：

```text
outputs/results/sessions/<session_id>/
  session.json
  takes/take_0001.json
```

Session 名称形如 `20260818-cup_stacking-001`，对应的默认 bag 名包含
`cup_stacking_take_0001`，方便从目录名判断任务和条次。

Take 包含 bag path、起止时间、trigger、quick QC、station check、operator accept/reject、
reject reason 与 anomaly timeline。说“本条作废”只写入 operator reject，原始 MCAP 永远保留。
Sessions 页面中的 **Open rosbag** 使用 Take 保存的 bag path 定位录制，因此切换 Recorder
写入目录后仍可打开旧 Take。

## 采集中

没有 Dashboard/Kiosk viewer 时处于 Capture Mode：不编码 JPEG、不启动 WebRTC worker、
不解析/叠加 hand display；图像 DDS reader 仍服务 header audit、相机新鲜度和两路 Insight3
2 Hz localization relay。相机、定位、丢帧和存储异常持续超过阈值后会主动语音提醒并写入
当前 Take，但不会自动删除 raw bag。

## 采后查看

打开本机 Firefox/Kiosk 或远程 Web 后，首个 viewer lease 启动 preview/WebRTC。最后一个
viewer 离开并空闲 45 秒后，WebRTC 和 hand-overlay worker 自动停止。采后再运行完整性、
回放、WiLoR/Ego LeRobot、UMI/LeRobot 导出和其他重处理。

固定治具检查由 capture profile 的 `station_check_after_take` 控制；自由运动任务不强制
每条回到固定 pose。Dense Mapping、RViz 和 COLMAP 属于 Engineering/Advanced，不进入
数采员现场主流程。
