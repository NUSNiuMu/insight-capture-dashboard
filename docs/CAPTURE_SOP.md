# 无屏背包采集流程

## 上电与 Preflight

上电后说“系统状态”。系统检查三相机图像新鲜度、三路 pose、Insight9 mapping、两路
Insight3 localization、录制存储可写性和剩余空间，以及必要 recorder topics。只有结果
通过时“开始录制”才会启动原生 MCAP recorder；失败原因直接由音响播报。

## Session / Take

每次录制属于一个 Session 和 Task，并分配递增的 `take_id`。元数据保存在：

```text
outputs/results/sessions/<session_id>/
  session.json
  takes/take_0001.json
```

Take 包含 bag path、起止时间、trigger、quick QC、station check、operator accept/reject、
reject reason 与 anomaly timeline。说“本条作废”只写入 operator reject，原始 MCAP 永远保留。

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
