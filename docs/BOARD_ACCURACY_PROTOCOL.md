# Insight9 标定板参照精度测试

本规范评估 **Insight9 建图输出的相机轨迹**：固定一块 ChArUco 板，从录制图像恢复相机相对板的位姿，与同期全局位姿比较。板必须固定，起始参考段只对齐一次，后续不重对齐、不拟合尺度或时间偏移。离线脚本不连接 ROS、不改地图、不启停录制；与 Ego 系统无关。

## 方法依据与适用范围

采用公开研究中的轨迹误差定义和 OpenCV 的板检测方法，以下是参考依据，不是认证或通用合格限值：

| 依据 | 本工具采用的部分 | 本工具的明确区别 |
|---|---|---|
| [TUM：Sturm 等，IROS 2012](https://cvg.cit.tum.de/_media/spezial/bib/sturm12iros.pdf) | 全局位置误差与相对位姿误差；报告 RMSE | TUM 使用独立动捕真值；这里是同一相机观测标定板得到的参照 |
| [UZH：Zhang、Scaramuzza，IROS 2018](https://rpg.ifi.uzh.ch/docs/IROS18_Zhang.pdf)、[官方评估工具](https://github.com/uzh-rpg/rpg_trajectory_evaluation) | 明确时间对应、坐标系、传感器尺度和对齐范围 | 此处固定起始窗口的完整姿态 SE(3) 对齐；不调用其全轨迹拟合工具。VIO 重力方向本身不是本测试对象 |
| [OpenCV 4.11 ChArUco](https://docs.opencv.org/4.11.0/df/d4a/tutorial_charuco_detection.html) | 带编号角点、亚像素角点和板位姿估计 | 固定一种板；不提供棋盘/其他码的替代分支 |
| [OpenCV solvePnP](https://docs.opencv.org/4.x/d5/d1f/calib3d_solvePnP.html) | 平面 IPPE 求解、检查多解 | 重投影无法区分的平面解直接拒绝，不用待测轨迹选择“更接近”的解 |
| [ROS REP-103](https://github.com/ros-infrastructure/rep/blob/master/rep-0103.rst) | 米、弧度、右手系；光学轴 x 向右、y 向下、z 向前 | 显式提供被测位姿到图像光学坐标系的刚体外参 |

**能说明**：板可见时的轨迹位置/角度误差、短时相对运动误差，以及离开后返回板前的偏移。**不能说明**：板不可见期间的全轨迹精度、整屋地图点云形状精度、两个 Insight3 的重定位精度或 TCP 精度。测试其他相机必须独立配置其图像、全局位姿与外参，不能套用头部结果。

板参照依赖打印尺寸、平整度、内参、外参和图像时间同步，与被测系统存在相关误差。需要整段独立真值时，应使用同步动捕、测量机器人或经测量的控制点方案；不能把本报告称为动捕级绝对精度。

## 先准备板、参数与录制

本机统一在现有容器执行，仓库挂载目录为 `/workspaces/insight_capture`。以下命令从仓库根目录运行；所有输出目录都必须是新目录，脚本不会覆盖已有报告。

```bash
docker exec -w /workspaces/insight_capture insight-dashboard \
  python3 -m insight_capture.postprocess.quality.board_accuracy make-board \
  --config config/devices/jetson-nx/board_accuracy.json \
  --output outputs/accuracy_board_v1
```

打印 `outputs/accuracy_board_v1/board_A3.pdf`：A3 横向、100% 实际大小，关闭“适合页面”。板为 **7×5 个方格（不是角点数）**，方格 40 mm、标记 28 mm、DICT_4X4_50；有效板面 280×200 mm，非旧版 ChArUco 图案。PNG 只用于屏幕检查，物理尺寸以 PDF 和实测为准。

粘贴到哑光刚性平板，避免起皱、弯曲与反光。用尺或卡尺检查 100 mm 校验线，并分别测量板面横向 280 mm、纵向 200 mm。记录实测值和量具精度。打印/测量不确定度应显著小于目标误差；例如要判断毫米差异，普通打印纸与粗略尺量通常不够。尺寸或平整度不达本次要求时重新制作，不能事后调尺度美化结果。

从实时 topic 导出一份独立评估配置及原始 TF 快照；不要修改正在运行的相机配置：

```bash
docker exec -w /workspaces/insight_capture insight-dashboard /entrypoint.sh \
  python3 -m tools.mapping_validation.capture_board_extrinsic \
  --config config/devices/jetson-nx/board_accuracy.json \
  --camera-config config/cameras.json \
  --output outputs/accuracy_run/config.json
```

配置中的 `camera.pose_from_rectified_image` 是 `T_poseBody_rectifiedImage`，满足：

```text
p_poseBody = T_poseBody_rectifiedImage × p_rectifiedImage
T_map_image = T_map_poseBody × T_poseBody_rectifiedImage
```

它是 4×4 齐次矩阵，平移单位米，不含尺度。来源填 `extrinsic_source`，例如厂家标定文件版本、序列号、标定日期和计算链。头部 `/insight9_sparse_map/pose` 表示双目中心；RGB 图像的 `insight9_a_camera_rgb` 是另一坐标系。**模板保留 null 是有意的，不能填单位矩阵绕过检查。** 若图像经过非单位 R 校正，外参还必须包含原始光学轴到校正光学轴的变换。

导出工具读取当前 cameras.json 的 ROS_DOMAIN_ID，在 rclpy.init 前设置；以 RELIABLE / TRANSIENT_LOCAL 订阅 `/tf_static`，读取左目→右目、左目→RGB，再结合实时 RGB CameraInfo.R 算出所需变换。它采用与 Insight9 mapper 相同的中点定义，15 秒内数据不全就报错，不用零外参替代。输出配置旁的 `_tf.json` 保留两个原始变换、相机 R/P、发布者和读取时间，其 SHA256 写入配置。每次重新接设备或更换标定后重新导出；归档时同时保存配置和快照。

这使用设备发布的厂家外参，不是在现场重新估计外参。PoseStamped 没有 child_frame_id，`pose_body_frame` 是操作者确认的接口约定，脚本无法从消息自动证明外参正确。用错方向、误用 IMU 外参或遗漏光学轴变换都会污染结果。

2026-10-27 已只读实测实时头部 TF：双目基线 100.160509 mm；推导 `T_center_rgb` 的平移为 `[-0.057925, 0.020649, -1.171018] mm`、旋转角约 0.304891°，RGB 的 R 为单位矩阵。当日完整矩阵及原始 TF 在本机 `outputs/board_accuracy_live_20261027/config.json`、`config_tf.json`；这是当时所连接设备的快照，不能当作所有 Insight9 的通用参数。工具不会把这些设备实测值硬编码到模板。

录制必须包含显式指定的图像、对应 CameraInfo 和 PoseStamped。仅支持已校正的针孔图像、CameraInfo.P 的 3×3 内参及零第四列；不对原始鱼眼自行去畸变。图像支持已有解码器的 ROS Image 格式及 JPEG/PNG CompressedImage，不支持 H.264/H.265。图像尺寸、frame_id、内参在一条录制中必须保持不变。

图像与位姿必须已使用同一源时钟；脚本只用 header.stamp，不用 rosbag 写入时间替换、不搜索时间偏移。任何时钟重置、回跳、重复图像/位姿时间戳或地图坐标系切换都应拆成不同录制。

## 现场采集流程

以下距离、时段、次数是本项目的可重复采集规程，**不是论文规定或行业验收值**。保持环境纹理、光照、相机配置、固件和算法版本一致，记录板实测尺寸、距离、轨迹、是否启用回环、测试人员和日期。

1. 三台相机固定安装后，在手机页面完成建图校准，确认头部建图与所需手部定位已就绪。校准完成后再录制；测试途中不重置地图、不移动板、不重新标定。预览可用于构图，不能替代录制文件检查。
2. 板固定在环境中，相机与板距离建议从 0.6–1.2 m 试起。让板占有足够像素，至少清晰检测 12 个不共线角点。光轴与板法线保持约 20–35°，避免完全正对产生平面位姿歧义。不能把板拿在手里跟随相机移动。
3. 开始采集后，以**第一条图像源时间戳**为 0 秒。时间线以配置为准；手机点击存在延迟，动作转换处预留余量。默认录制至少 112 秒：

| 图像相对时间 | 动作 | 用途 |
|---|---|---|
| 0–5 s | 设备与板均保持静止，板清晰可见 | 只用于一次 SE(3) 对齐，不计入精度分数 |
| 5–10 s | 保持静止，等待 | 转换余量，不评分 |
| 10–40 s | 保持静止 | `static`：静态误差与抖动表现 |
| 40–45 s | 准备移动 | 不评分 |
| 45–85 s | 缓慢左右、上下、前后移动并改变朝向，尽量持续看见板 | `motion`：姿态和相对运动误差 |
| 85–90 s | 离开板视野走一小圈，返回后稳定对准板；来不及时延长此段并在采集前修改配置 | 不对板不可见路段评分 |
| 90–110 s | 返回板前，保持静止且板可见 | `return`：返回后误差；保持最初对齐，不二次拟合 |

4. 停止并等待保存完成。至少独立重复 5 次，保存所有尝试，包括失败项；比较版本时使用相同配置、同一块板、相近距离和相同动作。每个版本分别报告每次结果、有效率、失败次数；不要只挑最好一次。不同距离可建立独立测试组。
5. 起始静止段出现大于 10 mm / 2° 的板参照散布时，工具判定参考段无效；检查固定、打印、检测和光照后重采，不能使用整段轨迹重新拟合来消除误差。

若现场时间无法严格控制，先明确动作段的图像时间，再冻结配置并用同一配置比较所有候选版本。不能根据误差高低挑选窗口。中途板不可见，报告中会保留失败原因；返回片段仍能用最初参考系衡量累积偏移。

## 检查与执行

先用现有完整性检查确认包已保存并检查图像/VIO 质量，再检查评估输入；脚本的 `inspect` 会核验明确配置的三个话题、源时间戳、frame、固定 CameraInfo，并输出计数与时间范围：

```bash
docker exec -w /workspaces/insight_capture insight-dashboard \
  python3 -m insight_capture.postprocess.quality.board_accuracy inspect \
  --config outputs/accuracy_run/config.json \
  --bag rosbags/insight9_mobile/实际录制目录

docker exec -w /workspaces/insight_capture insight-dashboard \
  python3 -m insight_capture.postprocess.quality.board_accuracy evaluate \
  --config outputs/accuracy_run/config.json \
  --bag rosbags/insight9_mobile/实际录制目录 \
  --output outputs/accuracy_run/result_01
```

路径按实际存储位置填写。`--bag` 是单条已完成 rosbag2 目录，内含 metadata.yaml；不指向总录制目录或单独 MCAP 文件。源包只读，输出不能写入源包。依赖均已在现有镜像中，无需安装新算法包或启动新服务。

结果包括 `report.md`、`summary.json`、逐帧 `samples.csv` 和 `errors.png`。JSON 保存完整配置、输入 metadata/配置/脚本 SHA256、数据文件大小及修改时间、OpenCV/NumPy 版本、对齐矩阵和质量门限。文件大小/mtime 不等于整个包内容的密码学指纹；正式归档可另行对所有包文件计算 SHA256。

## 指标与判断

固定板坐标系记为 B，地图记为 M，校正图像光学坐标系记为 C。PnP 给出 `T_C_B`；反转得到板参照 `T_B_C`。参考段逐帧计算 `T_B_M = T_B_C × inverse(T_M_C)`，对旋转作 SO(3) 均值、平移取均值，只得到一份固定对齐矩阵。这与对整段轨迹做最小二乘 ATE 对齐不同，目的在于保留起始之后的偏移；报告名称为“起始段 SE(3) 对齐的标定板参照误差”。

- 位置误差：`inverse(T_B_C_ref) × T_B_C_est` 的平移范数，单位 mm。
- 角度误差：同一误差变换旋转部分的 SO(3) 测地角，单位 deg；不是欧拉角逐项相减。
- 1 s RPE：比较相隔 1 s 的相对运动，`inverse(inverse(ref_i) × ref_j) × (inverse(est_i) × est_j)`；配对时间误差最多 50 ms，不跨片段、检测失败或采样缺口。位置与角度都报告 RMSE、中位数、P95、最大值和配对数。
- 有效率：有效配对数 / 计划 5 Hz 采样数（实际采样更多时用实际数），将图像缺口和识别/位姿失败计入分母。不能只报成功检测帧的误差。

质量门限固定：5 Hz 采样、至少 12 个非共线角点、角点凸包至少占图像 0.2%、重投影 RMS ≤1 px、最佳与次佳正深度 IPPE 解误差差值 ≥0.15 px、位姿插值两端间隔 ≤50 ms；只作线性平移与四元数最短弧插值，不外推。门限是本工具的工程设置，不代表计量不确定度或精度达标。运动应足够慢，避免滚动快门/时间偏差主导结果。

参考段至少 15 个有效配对，每个评估段至少 20 个有效配对、录制覆盖整个声明区间，且至少有一组连续 1 s RPE 配对；不满足则 `INVALID`，返回码 2，保留能生成的诊断报告。输入配置/外参缺失等前置错误只输出明确错误，不生成伪结果。`summary.json` 的总状态优先于片段统计，INVALID 时任何局部统计都不能用于验收。

未设验收阈值时为 `MEASURED_NO_THRESHOLDS`，返回码 0，仅表示完成测量。项目确定误差预算后，可在配置填入以下三个正值：

```text
acceptance = {
  "position_p95_mm": 项目允许的位置P95,
  "rotation_p95_deg": 项目允许的角度P95,
  "min_valid_fraction": 项目要求的最低有效率（0到1）
}
```

三个评估段必须分别满足全部条件，且数据有效，才为 PASS（返回码 0）；任一超限为 FAIL（返回码 3）。没有“通用 1 cm 合格”之类默认标准。先报告基线和打印/标定不确定度，再根据实际使用距离与任务要求确定门限。

部署时已有的短试录可检查数据格式，其文件内未保存 RGB 相对双目中心完整 TF；实时 topic 已读到并导出这项外参。短试录没有按此规程采集板，仍不能据此给出现有系统的精度数值。

## 工具验证记录（2026-10-27）

- 用真实 MCAP 读取链路和实际 OpenCV 检测器验证合成 ChArUco 图像，包含非单位相机外参、非单位地图变换、位姿插值、遮挡和已知运动。10 组评估/配置用例覆盖正常测量、漂移、尺度误差、阈值通过/失败、缺外参、全程无板、时钟回跳、内参变化和时段截断；另验证四元数正负号等价与正对平面的歧义拒绝。
- 注入 15 mm/s 平移漂移后，1 s RPE 位置 RMSE 为 14.99 mm；注入尺度误差后误差未被对齐消除。上述是算法与数据接线验证，不是现场设备精度。
- A3 PDF 页尺寸及打印预览检查通过；读取实时 TF 导出的完整矩阵与独立计算一致。旧试录实际读取 175 帧图像、173 条全局位姿；执行评估正确输出 INVALID，未生成虚假的精度结论。
- 证据在 `~/workspaces/insight_capture_tests/board_accuracy_20261027/`。此次未新起现场录制、未重置地图或重启服务。
