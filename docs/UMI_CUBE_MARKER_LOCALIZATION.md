# UMI cube marker 相对定位

## 官方几何与编号

本 profile 使用 UMI 官方 `DICT_4X4_50` 打印配置和 CAD：

- cube 外边长：70 mm；
- cube/body marker 黑框边长：60 mm，每面四周各留 5 mm；
- 右夹爪 `insight3_a` 的实体 cube 为 ID 8、9、10、11；
- 左夹爪 `insight3_b` 的实体 cube 为 ID 2、3、4、5。

UMI 原始算法没有使用大 cube 上的四个 marker。本项目把它们作为一个刚体目标，
联合估计 cube 位姿，再转换到 Insight3 双目中心。

## Cube 坐标系

坐标系原点位于 cube 几何中心，方向与 Insight3 `camera_center` 光学坐标保持一致：

- `+X`：从夹爪相机画面看向右；
- `+Y`：从夹爪相机画面看向下；
- `+Z`：从 cube 指向夹爪相机及指尖方向。

四面 marker 的配置约定为：

| 面 | `insight3_a` ID | `insight3_b` ID | Cube 坐标 |
| --- | --- | --- | --- |
| 顶面 | 8 | 2 | `Y=-35 mm` |
| 后面 | 9 | 3 | `Z=-35 mm` |
| 左面 | 10 | 4 | `X=-35 mm` |
| 右面 | 11 | 5 | `X=+35 mm` |

每个 `corners_cube_m` 都严格按 OpenCV ArUco 返回的
`top-left, top-right, bottom-right, bottom-left` 顺序填写。ID 8 的印刷旋转以及
ID 9/10/11 所在面已经用 Take 48 的真实 Insight9 RGB 和全局 pose 核对。

## 当前外参与融合状态

4x4 AprilGrid 标定板对左右夹爪统一使用同一套角点尺度后，当前平移配置为：

```text
insight3_a T_cube_camera_center.translation = [0.000, -0.058, 0.090] m
insight3_b T_cube_camera_center.translation = [0.000, -0.053, 0.093] m
rotation = identity
```

Jetson NX profile 当前设置：

```json
"enabled": true,
"apply_corrections": true
```

二维码候选先通过 5 帧窗口内 3 帧一致性检查，再作为 `T_map_odom` 的低频绝对观测
写入 Insight3 的六自由度误差状态 EKF。小于 150 mm 且小于 10 度的修正由 EKF
平滑吸收；超过任一阈值的已确认修正会执行 hard relocalization。Insight3 VIO 仍负责
高频相对运动，最终输出为 `T_map_odom * T_odom_imu * T_imu_camera_center`。

## 验证与已知限制

Take 54 使用当前独立外参回放时，B 的局部相对误差中位数为 30.1 mm / 2.59 度；
A 为 60.0 mm / 5.31 度，并出现过平面 PnP 错误分支。当前配置允许单 marker
连续三帧确认，因此生产观测仍需关注 `hard_relocalizations`、marker ID 组合和换面时的
位姿连续性。

离线验证使用 `tools/diagnostics/replay_cube_marker_shadow.py`。该工具始终只读，即使加载
的生产配置是 `apply_corrections=true`，也只输出 shadow 统计，不修改任何在线 pose。
