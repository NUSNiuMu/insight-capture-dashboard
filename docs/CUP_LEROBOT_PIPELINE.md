# 一次性纸杯双夹爪 LeRobot 生产流程

本流程用于把一条或多条连续夹取录制转换成原始分辨率的 LeRobot v3 数据集。目标是累计 500 条可训练 episode，同时保留源 rosbag、剔除位姿不连续片段、排除人工复位和纯空闲区间，并提供逐帧原子动作标签。

## 采集约定

- 每个 rosbag 连续采集约 10–20 次完整动作，不要把 500 次全部录在一个包里。
- 每次动作采用：初始张开 → 接近纸杯 → 闭合夹持 → 抬起并搬运 → 放入纸箱并张开 → 撤离。
- 每次完成后让两个 TCP 和夹爪稳定至少 1 秒，再由人工复位纸杯。
- 人工复位时不要移动夹爪；下次动作开始前再次停稳。
- 每次动作必须以夹爪张开开始并以张开结束。失败夹取应单独重录，不要混在成功数据中。
- 建议每录完一个包立即在 Dataset 页面运行生产导出；只有通过质量门的 episode 才计入 500 条目标。

源 rosbag 始终保留。只有整包无法生成任何有效 episode 且属于录制数据质量问题时，网页批处理才会给源目录增加 `fail_` 前缀；部分 episode 合格时不会改名。

## 网页批处理

打开 `/umi-dataset`：

1. 选择一个或多个 rosbag。
2. Dataset format 选择 **LeRobot v3**。
3. Gripper episode segmentation 选择 **Production cup grasps · filter, label, verify**。
4. 填写完整英文任务，例如 `Pick up a disposable cup with both grippers and place it into the box.`。
5. 该模式会自动锁定双臂三相机布局和 **Original resolution**，不能选择 224 或 384。
6. 点击导出。每个源包保存为独立的 `<rosbag>_cup_lerobot/` 数据集，便于失败重试和分批管理。

所有已导出的纸杯数据集会汇总到：

```text
outputs/lerobot_datasets/cup_catalog.json
```

目录会记录当前有效 episode 总数、距离 500 条还差多少、总帧数、时长、大小以及每个分片的验证状态。

## 命令行

```bash
docker exec -w /workspaces/insight_capture insight-dashboard \
  bash -lc 'source /opt/ros/humble/setup.bash && \
  python3 scripts/cup_lerobot_pipeline.py \
    rosbags/insight_record_20260811_115900 \
    --output outputs/lerobot_datasets/insight_record_20260811_115900_cup_lerobot \
    --task "Pick up a disposable cup with both grippers and place it into the box." \
    --dataset-id insight/insight_record_20260811_115900_cup_lerobot \
    --camera insight3_a --camera insight3_b --camera insight9_a'
```

生产脚本只接受 `--image-size original`，不会裁剪或缩放图像。

## 自动处理阶段

1. 解码三路图像、双臂全局位姿和实测夹爪宽度。
2. 使用至少 0.8 秒的静止段把长录制切成候选小节。
3. 用位置步长和局部速度创新、姿态步长及 pose 间隔检查 VIO 重置或跟踪空洞。
4. 只保留夹爪宽度具有完整“张开—闭合—张开”循环且变化范围至少 3 cm 的候选段。
5. 按主闭合动作前 2 秒、释放后 2.5 秒裁剪上下文，排除停顿期间的人工纸杯复位和冗余空闲。
6. 以 20 Hz 对齐三路原始分辨率图像、双臂 TCP、旋转和夹爪宽度。
7. 写入 LeRobot v3 Parquet、三路 H.264/yuv420p 视频、同步时间戳与 validity mask。
8. 写入五类逐帧 `task_index` 和 `meta/segments.parquet`。
9. 完整解码三路视频，并检查 episode、时间戳、状态/action、动作段覆盖和任务索引。
10. 生成质量报告、验证报告、代表帧联系表并更新累计 catalog。

## 动作标签

| task_index | subtask | atomic_action | 英文训练指令 |
|---:|---|---|---|
| 0 | reach | approach_cup | Move both grippers toward the disposable cup. |
| 1 | grasp | close_grippers | Close both grippers around the disposable cup. |
| 2 | transport | transport_cup_to_box | Lift and carry the disposable cup to the box. |
| 3 | release | open_grippers | Open both grippers to release the disposable cup into the box. |
| 4 | retreat | retreat_from_cup | Move both grippers away from the released cup. |

每个 episode 的五段标签连续覆盖全部帧，无空洞或重叠。完整任务保存在 `meta/manifest.json` 的 `full_task`；训练可以使用逐帧 `task_index` 做原子动作语言条件，也可以使用完整任务做整条 episode 条件。

## 每个输出分片

```text
<rosbag>_cup_lerobot/
  data/chunk-000/file-000.parquet
  videos/observation.images.base_0_rgb/chunk-000/file-000.mp4
  videos/observation.images.left_wrist_0_rgb/chunk-000/file-000.mp4
  videos/observation.images.right_wrist_0_rgb/chunk-000/file-000.mp4
  meta/info.json
  meta/manifest.json
  meta/modality.json
  meta/tasks.parquet
  meta/segments.parquet
  meta/quality_report.json
  meta/verification.json
  review/atomic_action_contact_sheet.jpg
```

`meta/quality_report.json` 记录被保留、因不完整夹取排除、以及因位姿不连续拒绝的源段。`meta/verification.json` 必须为 `PASS` 才能计入 catalog。

## 500 条数据的管理建议

- 以 rosbag 为可恢复分片，每包 10–20 条，预计需要约 25–50 个包。
- 不要在一次导出中手工拼接或移动源包；让网页按包生成独立数据集并更新 catalog。
- 每批结束后抽查 `review/atomic_action_contact_sheet.jpg`，确认接近、夹持、搬运、释放、撤离五列语义正确。
- 达到 500 条时以 `cup_catalog.json` 中 `verification=PASS` 的分片作为训练输入清单。
- 原始分辨率视频占用显著高于 224×224；采集前后都应检查磁盘余量，并保留 rosbag 的独立备份。
