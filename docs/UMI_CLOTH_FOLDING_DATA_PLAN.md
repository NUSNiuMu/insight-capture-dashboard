# UMI 双臂叠衣服数采与训练适配计划

## 目标

使用人体佩戴设备采集双臂叠衣服示范，并将数据转换为可训练、可审计、可部署到
双臂平行夹爪机器人的 UMI-style 数据集：

```text
Insight9 头部 RGB
+ 左右 Insight3 红外图像与全局位姿
+ 左右 UMI gripper
  -> 原始 ROS bag
  -> 定位与采集质量门禁
  -> 离线夹爪开合解析
  -> camera-center 到 TCP 转换
  -> 20 Hz 多流对齐与相对动作生成
  -> UMI Zarr / LeRobot v3
  -> 双臂叠衣服策略训练与部署验证
```

ROS bag 始终是不可变的原始事实源。定位 mask、ArUco 修复、图像裁剪、动作表示和
数据集格式都属于可重新执行的派生处理，不覆盖原始图像或位姿。

## 已确认的产品边界

- 数据来自人体示范，不要求采集 `/robot/joint_states` 或 `/robot/command`。
- 左右手各持一个与机器人平行夹爪相近的 UMI gripper。
- Insight9 位于头部，提供全局任务视角和地图坐标系。
- Insight3 固定在左右 UMI gripper 上，首版使用 `infra1` 左目整流图像。
- 夹爪开合不进入实时录制关键路径，首版从 rosbag 图像离线解析。
- 首个任务只覆盖一种固定的双臂衣物折叠流程，不同时解决多任务泛化。
- 第一训练目标采用 UMI-style 相对 TCP 轨迹；LeRobot 是并行存储/训练适配层，不能
  改变动作语义。

## 当前数据源

当前 Recording 页默认选择的相关 topic 如下：

| 角色 | Topic | 首版用途 |
| --- | --- | --- |
| 头部图像 | `/insight9_a/camera/color/image_rect_raw/compressed` | 全局视觉 observation |
| 头部位姿 | `/insight9_sparse_map/pose` | 质量审计；活动头部署时可选 observation |
| 右腕图像 | `/insight3_a/camera/infra1/image_rect_raw` | 右腕视觉 observation、离线夹爪解析 |
| 右腕全局位姿 | `/insight_global/insight3_a/pose` | 右 TCP 轨迹来源 |
| 左腕图像 | `/insight3_b/camera/infra1/image_rect_raw` | 左腕视觉 observation、离线夹爪解析 |
| 左腕全局位姿 | `/insight_global/insight3_b/pose` | 左 TCP 轨迹来源 |
| 相机内参 | 三路 `camera_info` | 图像几何和标定版本 |
| 静态外参 | `/tf_static` | 坐标变换审计 |
| 定位质量 | 三路 `vio_image_cov` | 轨迹质量门禁 |
| IMU | 三路 `/camera/imu` | 时间和运动质量审计，不默认输入策略 |

录制前必须确认左右 `/insight_global/.../pose` 持续发布。只有 VIO、没有成功全局定位的
episode 不得默认进入训练集。

## 核心数据合同

### 坐标系与 TCP

TCP 是夹爪的完整 6DoF Tool Center Point，不只是相机到某个指尖的距离。平行夹爪
首版约定：

- 原点：两个指尖接触面的中点；
- `+Z`：夹爪向前伸出的方向；
- `+X`：两指开合方向；
- `+Y`：按右手系确定；
- 左右 TCP 坐标系采用相同语义，不因左右手安装而偷偷镜像。

当前全局位姿输出 frame 是 `*_global_camera_center`，因此每个 UMI 必须标定固定变换：

```text
T_map_tcp(t) = T_map_camera_center(t) * T_camera_center_tcp
```

如果标定工具直接输出左目光心到 TCP 的变换，则需利用设备外参组合：

```text
T_camera_center_tcp = T_camera_center_infra1 * T_infra1_tcp
```

位置单位统一为米，旋转内部计算使用旋转矩阵或归一化 `xyzw` 四元数，训练 feature
使用 continuous rotation 6D。每份标定必须带设备序列号、左右角色、时间、方法和
版本；更换安装位置后必须生成新版本。

### 图像

首版固定使用左目整流图像，不对“偏向一侧”的光心视角做视觉居中。训练和部署必须
使用一致的：

- 左目流；
- 相机安装位姿；
- 内参与畸变校正；
- 分辨率、裁剪、旋转和 FOV；
- 左右相机 feature 名称。

不默认镜像任一路图像。若后续为数据共享而镜像，必须同时变换内参、TCP/action
坐标和左右语义，并升级 schema version。

### 夹爪开合

离线 extractor 为左右手逐帧输出：

```text
source_timestamp_ns: int64
opening: float32              # 0.0 closed, 1.0 open
opening_raw_px: float32
left_marker_found: bool
right_marker_found: bool
valid: bool
confidence: float32
```

当前标记合同固定为 ArUco `DICT_4X4_50`，左指 ID 1、右指 ID 0。必须用 Insight3 红外
实拍验证打印材料的对比度和反光，不以 RGB 相机可见性代替红外验收。

开合标定至少包含全开和全闭距离，并分别绑定左右相机。小于等于配置阈值的短缺失可
插值；关键抓取阶段长缺失必须拒绝 episode，禁止无限保持最后一次有效值。

### UMI-style observation 与 action

对齐后的标准 frame 首版包含：

```text
observation.images.head_rgb       uint8 [H, W, 3]
observation.images.right_wrist    uint8 [H, W, 3]
observation.images.left_wrist     uint8 [H, W, 3]
observation.state                 float32 [20]
action                            float32 [20]
```

每只手 10 维：

```text
tcp_position_xyz(3) + tcp_rotation_6d(6) + gripper_opening(1)
```

首版采用相对 TCP 表示。绝对 `T_map_tcp` 仍保存在审计输出中，但不直接作为策略 action：

```text
T_right_relative(t, t+k) = inverse(T_right(t)) * T_right(t+k)
T_left_relative(t, t+k)  = inverse(T_left(t))  * T_left(t+k)
```

双臂同步器还需计算 `inverse(T_right) * T_left` 作为质量检查和可选 observation，保证
模型能表达拉平、保持张力和双手合拢等关系。

首版时间参数：

```text
dataset_fps: 20
image_observation_horizon: 2
low_dim_observation_horizon: 2
action_horizon: 16
```

LeRobot 每行存一个时刻的 20 维 action；未来 16 步 action chunk 由训练数据加载器按
timestamp 形成。UMI exporter 按官方 replay buffer 的 horizon 约定输出，不在两个
exporter 中复制不同的动作定义。

### Episode 元数据

每个 bag 对应一个完整示范 episode：

```json
{
  "task": "fold_tshirt_v1",
  "instruction": "Fold both sides inward, then fold the shirt in half",
  "success": true,
  "operator": "operator_01",
  "garment_type": "short_sleeve_tshirt",
  "scene": "folding_table_01",
  "schema_version": "umi_cloth_v1",
  "calibration_version": "...",
  "notes": ""
}
```

episode 从双夹爪打开且未接触衣物开始，到最终折叠完成、双夹爪松开并离开衣物结束。
失败、中止和质量不合格的数据保留在原始层，但不默认进入训练 split。

## 定位夹爪 Mask 设计

左右 Insight3 分别提供一张与 `infra1/image_rect_raw` 同分辨率的 mask。mask 覆盖：

- 固定夹爪基座；
- 两个 ArUco 标记；
- 指爪从全闭到全开的运动包络；
- 包络边界外额外膨胀 5 至 15 px。

数据路径必须分离：

```text
raw image
  +-> localization view: keypoint mask，仅用于 SuperPoint/global localization
  +-> policy raw view: 保留完整图像
  +-> policy cleaned view: 可选 ArUco inpaint/固定基座 mask
```

优先在 SuperPoint 返回 keypoint 后过滤 mask 内特征，不直接把输入图像涂黑，以免在
mask 边缘制造人工特征。mask 不影响录入 rosbag 的原始图像。

首版同时保留 raw/cleaned 导出能力，但训练只选择一个固定 observation schema。
对叠衣服任务，默认 inpaint ArUco 和固定基座，保留真实运动指爪及其与布料的接触
关系；最终选择需通过小规模对照实验确认。

## 离线处理流水线

### 1. 原始完整性门禁

- 检查三路图像、左右 global pose、camera_info、`/tf_static` 和 covariance；
- 检查有效公共时间区间、掉帧率、图像 header 连续性和定位状态；
- 检查标定版本与设备序列号匹配；
- 失败时生成明确 rejection reason，不静默补齐。

### 2. 离线夹爪提取

- 直接读取 Insight3 `mono8/8UC1` rosbag 图像；
- 按 source header timestamp 保存检测结果；
- 生成带 marker、开合值和有效状态的低帧率 overlay 视频；
- 生成 opening/valid/confidence 时序图；
- 自动统计检测率、最长连续缺失和抓取阶段缺失。

实时 dashboard 可选仅以 1 至 2 Hz 检查 marker 可见性，不能成为录制关键路径或训练
标签的唯一来源。

### 3. TCP 轨迹生成

- 对 camera-center pose 去重并按 timestamp 排序；
- 应用固定 `T_camera_center_tcp`；
- 位置线性插值，四元数先做符号连续化再 SLERP；
- 输出绝对 TCP、相对 TCP、速度和角速度；
- 检查跳变、超速、越界和四元数范数。

### 4. 20 Hz 多流同步

- 在所有必需流的公共区间创建 `timestamp[i] = i / 20`；
- 图像采用最近曝光帧，记录每路匹配误差；
- pose 使用插值，不跨越定位断层；
- gripper 连续值使用短间隔插值；
- 保存 bag timestamp 和 source header timestamp 以供审计；
- 裁掉开头、结尾无动作段，但保留任务建立和最终释放所需上下文。

### 5. 动作与训练 feature 生成

- 由对齐后的绝对 TCP 计算相对 SE(3)；
- 将旋转转换为 rotation 6D；
- 固定左右顺序和 gripper 范围；
- 生成 20 维 state/action；
- 检查同一 schema 下所有 episode 的 dtype 和 shape 完全一致。

### 6. 双格式导出

建立单一 `AlignedEpisode` 中间接口：

```text
AlignedEpisode
  +-> UMI Zarr replay buffer
  +-> LeRobotDataset v3
```

- UMI exporter 负责复现官方 Diffusion Policy 数据接口；
- LeRobot exporter 使用官方 `create/add_frame/save_episode/finalize` API；
- 临时输出与最终数据集隔离，失败不得覆盖上一次有效结果；
- 两种输出需对同一抽样 frame 给出一致的图像、TCP 和 opening。

## 数据质量门禁

episode 只有全部满足以下条件才标记 `usable=true`：

- 三路图像可解码且帧数与 20 Hz 时间轴一致；
- 左右 global pose 全程有效，无未处理的定位重置或大跳变；
- TCP 标定存在且设备/角色匹配；
- 左右 ArUco 检测率和最长缺失满足阈值；
- 抓取、释放附近有可信 opening；
- state/action 无 NaN、Inf、维度变化和越界；
- 同步误差、速度、角速度和双手距离在合同范围；
- episode 元数据、task 和 success 完整；
- episode 长度和有效动作占比满足训练要求。

每个 episode 输出：

```text
usable
rejection_reasons
quality_metrics
source_bag
schema_version
calibration_version
```

阈值必须先通过真实短采样统计确定，不能只凭经验固化。

## 分阶段实施

### P0：现场验证与合同冻结

任务：

1. 确认左右 Insight3 红外画面能同时看见 ID 0/1。
2. 收集全开、半开、全闭和快速开合样例。
3. 确认三路图像与左右 global pose 的实际频率和时间戳语义。
4. 冻结 TCP 轴向、左右角色、开合范围和 `umi_cloth_v1` schema。
5. 确认机器人部署侧的腕相机视角与夹爪 TCP 定义。

验收：

- 红外 ArUco 在典型光照、运动和衣物遮挡下可检测；
- 任一训练 action 维度都能追溯到源 pose、标定或图像；
- 人体采集与机器人部署的 feature 语义一致。

### P1：定位 mask

任务：

1. 生成左右夹爪全运动包络 mask。
2. 在 Insight3 global localizer 的 feature 路径过滤 masked keypoint。
3. 在无遮挡、半遮挡、开合运动和衣物近距离条件下对比定位指标。

验收：

- mask 内 keypoint 不进入 PnP；
- 环境有效 matches/inliers 不下降到不可用范围；
- 开合动作不触发虚假重定位或明显增加 hard relocalization；
- 不改变录制和 policy 原始图像。

### P2：TCP 标定

任务：

1. 实现或接入左右 `camera_center -> TCP` 标定流程。
2. 保存版本化标定文件和标定残差。
3. 用已知工装点位验证静态位置与旋转误差。

验收：

- 重复拆装后的误差满足叠衣服所需精度；
- 左右 TCP 轴向和手性一致；
- 开合过程中 TCP 中点不发生不合理漂移。

### P3：Insight3 离线夹爪提取

任务：

1. 为现有校准工具补齐 `mono8/8UC1` 和 ROS `step` 解码。
2. 实现 rosbag 离线 ArUco extractor。
3. 生成检测缓存、质量统计、overlay 视频和曲线。
4. 定义短缺失插值与长缺失拒绝规则。

验收：

- 全开/全闭归一化方向正确；
- 同一 rosbag 重复执行结果确定；
- 人工抽样 opening 与图像一致；
- 离线处理不要求 dashboard 实时 gripper topic。

### P4：同步器与标准 episode

任务：

1. 实现 rosbag reader 和公共时间区间计算。
2. 实现 20 Hz 图像、pose、gripper 同步。
3. 实现 TCP 与相对动作生成。
4. 输出 `AlignedEpisode` 和质量报告。

验收：

- 每个 timestep 都有固定 shape 的三路图像、state 和 action；
- 视频帧数与低维数据行数严格一致；
- 任意样本能回溯到原始 ROS 消息和标定；
- 人为制造的断流、跳变和 marker 长缺失会被拒绝。

### P5：UMI 与 LeRobot 导出

任务：

1. 先实现 UMI Zarr exporter，跑通官方 UMI dataset loader。
2. 再实现 LeRobot v3 exporter。
3. 验证两个 exporter 的 feature 语义一致。
4. 导出过程放在独立进程且只在非录制期间运行。

验收：

- UMI loader 能取出正确 observation/action horizon；
- LeRobot 能加载完整数据集并生成 DataLoader batch；
- 两种格式抽样数值和图像一致；
- 导出失败不会产生被误认为有效的数据集。

### P6：叠衣服训练闭环

任务：

1. 固定一种短袖 T 恤折法和 episode 边界。
2. 先采 10 至 20 条调试数据，完成可视化和 overfit 测试。
3. 数据链路通过后采集首批 100 至 200 条成功示范。
4. 比较 raw wrist view 与 cleaned wrist view。
5. 在机器人上进行低速、软限位和人工急停保护下的 rollout。
6. 记录失败类型并定向补充数据，而不是无差别扩量。

验收：

- 小数据能过拟合，证明 action/observation 时序没有错位；
- 离线 rollout 可视化中的预测方向与示范一致；
- 真实机器人完成抓角、拉平、折边、对折和释放的完整链路；
- 训练和部署的相机、TCP、action horizon、控制周期与时延合同一致。

## 算力与运行约束

- 录制 callback 中不得加入 ArUco、JPEG 编码或磁盘后处理。
- SuperPoint/SuperGlue 定位只增加 keypoint mask，不增加新的全速图像订阅。
- 离线夹爪提取默认在录制停止后运行；录制期间不得自动并发导出。
- 如需要现场 marker 可见性提示，只以 1 至 2 Hz 运行并独立统计性能。
- 在 Jetson 上引入任何实时检测前，必须记录 CPU、GPU、内存带宽、图像掉帧和定位
  inference 时间的前后对比。

## 风险与降级策略

| 风险 | 影响 | 降级或处理 |
| --- | --- | --- |
| 红外下 ArUco 对比度不足 | opening 大量无效 | 更换红外友好材料、尺寸和安装角度 |
| 衣物遮挡标记 | 抓取阶段缺 opening | 调整标记位置；双面/冗余标记；拒绝长缺失 |
| 夹爪占据画面过大 | 定位环境特征不足 | 调整安装/FOV；mask 后评估有效视野 |
| camera-to-TCP 安装松动 | action 标签系统性漂移 | 防松结构、每次采集前快速标定检查 |
| 左目人体/机器人视角不一致 | 部署域差 | 统一硬件安装或明确做几何/视觉适配 |
| 头戴视角与机器人头相机不一致 | 全局视觉域差 | 首版以腕图为主，头图做可选消融 |
| 布料遮挡导致定位丢失 | TCP 轨迹中断 | 增强环境地图纹理，使用 VIO 短时续接并设严格门禁 |
| 人体动作不可达 | 机器人 rollout 失败 | 导出前做工作空间、速度、碰撞和双臂可达性过滤 |

## 首批待办

- [ ] 冻结 `umi_cloth_v1` TCP、图像和 action 合同。
- [ ] 采集 Insight3 红外 ArUco 可见性样例并统计检测率。
- [ ] 生成左右夹爪运动包络定位 mask。
- [ ] 在 Insight3 global localizer 中过滤 mask 内特征。
- [ ] 完成左右 `camera_center -> TCP` 标定。
- [ ] 修复 gripper calibration 的 `mono8/8UC1` 解码。
- [ ] 实现 rosbag 离线 gripper extractor 和质量报告。
- [ ] 实现单 episode 的 20 Hz `AlignedEpisode`。
- [ ] 实现双臂 20 维相对 TCP action。
- [ ] 导出 UMI Zarr 并完成 loader smoke test。
- [ ] 导出 LeRobot v3 并完成 DataLoader smoke test。
- [ ] 用 10 至 20 条数据完成 overfit 与机器人低速闭环验证。
