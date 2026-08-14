# UMI TCP 坐标系

## 坐标系定义

双臂数据使用两个固定语义的 ROS 坐标系：

| 手别 | `teleop_role` | TCP frame | Jetson NX 位姿源 | Lite 位姿源 | Lite 779 位姿源 |
| --- | --- | --- | --- | --- | --- |
| 右手 | `right_hand` | `right_tcp` | `insight7_a` | `insight3_a` | `insight7_a` |
| 左手 | `left_hand` | `left_tcp` | `insight7_b` | `insight3_b` | `insight7_b` |

左右 TCP 都采用相同的右手坐标系，不对左手做镜像：

- 原点：两个指尖接触面在夹爪中心线上的中点，不随开合量变化。
- `+X`：从相机画面看向右，平行于夹爪开合方向。
- `+Y`：从相机画面看向下。
- `+Z`：从相机光心指向指尖，即夹爪前向/接近方向。

这与 [UMI 官方数据管线](https://github.com/real-stanford/universal_manipulation_interface/blob/main/scripts_slam_pipeline/06_generate_dataset_plan.py#L99-L108)
一致：TCP 保持相机光学坐标系的旋转，相机到 TCP 只需标定平移。本仓库的
`camera_center` 同样保持左目光学坐标系方向，因此标定结果应为：

```text
T_map_tcp = T_map_camera_center * T_camera_center_tcp
T_camera_center_tcp.rotation = identity
```

每个设备 profile 都必须单独实测，不能复用旧 Insight3 或 UMI GoPro 的机械尺寸。
当前 Jetson NX 的 Insight7 A/B 尚未写入 `camera_center_to_tcp`，因此系统不会发布
对应静态 TCP TF，也不会用占位值冒充机械标定。

## 数据集约定

- 位置单位为米，四元数顺序为 `xyzw`。
- `tcp_pose` 表示 `T_map_tcp`，而不是 `T_tcp_map`。
- 后续 20 Hz 双臂数据固定按右手、左手排列；手别由 `teleop_role` 决定，
  不依赖设备名的字母后缀。
- 设备 profile 在 `config/devices/<profile>/cameras.json` 中用 `tcp_frame_id`
  显式记录相机与 TCP frame 的对应关系。
- LeRobot 双臂状态固定为 20 维 `[left_10d, right_10d]`；每臂依次为位置 `xyz`、
  连续旋转表示 `rot6d` 和夹爪宽度 `width`，长度单位均为米。
- 单臂录制使用对应一臂的 10 维布局。action 保存下一时刻绝对状态，而不是在归档层
  预先转成相对动作。

## 当前标定状态

Jetson NX 的两路 TCP 外参和米制夹爪宽度标定仍需按实物重新测量。双臂数据导出会在
关键标定缺失时安全失败，不应通过默认常数绕过检查。
