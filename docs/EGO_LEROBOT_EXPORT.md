# Ego 三视角 LeRobot 交付流水线

`insight_capture.postprocess.datasets.ego_lerobot.cli` 将一条三相机 rosbag 转换为当前 Ego 交付格式：三路图像全部交付，但手部检测与 3D 手姿只来自头部 RGB；左右腕部图像不运行手势模型。相机角色以 `config/cameras.json` 的 `teleop_role` 为准，当前映射是 `insight3_b → left_hand`、`insight3_a → right_hand`。

## 使用

动作裁剪与多段标注保存在版本化 JSON，而不是写死在转换脚本中：

```bash
docker exec insight-dashboard python3 -m insight_capture.postprocess.datasets.ego_lerobot.cli \
  rosbags/insight_record_20260807_144636 \
  outputs/lerobot_datasets/ego_hand/shirt_folding_20260807_144636_ego_lerobot_v2 \
  --spec config/dataset_schemas/ego_shirt_folding_20260807_144636.json
```

第一次执行需要解码、WiLoR 推理并编码三路 H.264。完成结果按 rosbag 指纹、标注文件、相机配置、模型与全部质量阈值写入 `outputs/ego_lerobot_cache/`。相同输入再次执行会验证缓存并以硬链接复用 MP4、复制元数据，不再重复推理和编码。基础视觉/手姿阶段使用独立缓存键，不包含任务文本和动作段；只修改标注时只重写 Parquet 元数据与 overlay，也不会重跑模型或转码。输出目录必须是新目录，避免误覆盖已交付数据。

已经人工验收的最终数据可以只在迁移期用于建立可信缓存：

```bash
docker exec insight-dashboard python3 -m insight_capture.postprocess.datasets.ego_lerobot.cli \
  rosbags/insight_record_20260807_144636 outputs/lerobot_datasets/ego_hand/<new_name> \
  --spec config/dataset_schemas/ego_shirt_folding_20260807_144636.json \
  --reuse-dataset outputs/lerobot_datasets/ego_hand/<accepted_dataset> \
  --reuse-overlay outputs/lerobot_datasets/ego_hand/<accepted_overlay>.mp4
```

`--reuse-dataset` 不会盲目复制：它会先检查 2116 帧、13 段连续标注、三路视频帧数/FPS、必须的手姿字段、仅头部手姿策略，以及相机参数中不存在 `left_tcp`/`right_tcp`。日常快速审计只读视频容器信息；交付前可加 `--decode-audit` 对三路视频逐帧完整解码。

## 左右手与质量约束

- WiLoR 检测器原始 handedness 保留，不在模型输入前翻转。
- 腕部相机的高精度 pose 只作为物理位置约束：把腕部相机中心投影到头部画面，默认距离手腕关键点超过 600 px 时拒绝该伪标。
- 投影约束后检查单帧轨迹连续性。当前规则仅处理“本帧只检出一只手、前一帧两手均有效”的明显身份误标，默认同标签跳变超过 0.15 m 且更接近另一轨迹至少 0.08 m 才移动左右槽位。
- 所有投影剔除与身份纠错写入 `meta/keyframes.parquet` 和 manifest，便于复核；不会伪造腕部画面的手势检测。
- `/tf_static` 只保留相机/IMU 标定，任何父子 frame 含 `tcp` 的变换都不会交付。

## 接入其他手姿模型

`--hand-backend` 支持 `module:factory`。factory 接收 `model_dir`、`confidence`、`focal_length` 三个关键字参数，返回实现以下接口的对象：

```python
class Backend:
    name: str
    version: str
    def cache_identity(self) -> dict: ...
    def predict(self, image_bgr) -> Sequence[HandPosePrediction]: ...
    def close(self) -> None: ...
```

预测必须归一化为 `ego_lerobot.model.HandPosePrediction`：原图 2D 关键点、相机坐标系 3D 关键点、手腕四元数、MANO 45 维姿态、置信度和原始 handedness。后续坐标变换、物理约束、身份纠错、LeRobot 写入、质检和 overlay 均与具体模型解耦。`cache_identity()` 必须包含权重版本或校验信息，模型变化才能正确使缓存失效。
