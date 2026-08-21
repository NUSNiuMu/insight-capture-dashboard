# 无屏背包采集流程

## 上电与 Preflight

上电后说“系统状态”。系统检查三相机图像新鲜度、三路 pose、Insight9 mapping、两路
Insight3 localization、录制存储可写性和剩余空间，以及必要 recorder topics。只有结果
通过时“开始录制”才会启动原生 MCAP recorder；失败原因直接由音响播报。

## Task Set / Take

数据按两级组织：Task Set 表示长期维护的动作任务集，Take 表示其中的一条录制。内置默认
任务配置在 `config/capture_tasks.json`；数采员可在 Sessions 页面新建和编辑任务，不需要改
配置或重启服务。任务集使用稳定的 `task_id` 文件夹，不随日期、服务重启或退出后重新进入
而重建；Task ID 创建后不可修改，Take 编号在同一任务集中持续递增。

Sessions 页面可新建、编辑、选择、进入和结束任务集；现场也可直接说“开始任务叠杯子”
“当前任务多少条”“结束当前任务”。录制过程中不能新建、编辑、切换或退出任务集。没有
当前任务时，网页和语音录制都会被拒绝，不会产生非任务 bag。元数据保存在：

```text
outputs/results/sessions/<task_id>/
  task.json
  session.json
  takes/take_0001.json
```

`task.json` 是前端管理的持久任务定义，与发布镜像中的默认配置分离，因此升级后仍会保留。

对应 raw bag 写入当前 Recorder 存储根目录下的 `<task_id>/`，例如
`rosbags/cup_stacking/cup_stacking_take_0001_.../`。旧版当前日期 Session 的轻量元数据会在
升级后迁移到稳定任务集目录；已录制 raw bag 不移动、不改名。

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
