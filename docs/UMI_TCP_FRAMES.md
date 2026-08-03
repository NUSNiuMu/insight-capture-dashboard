# UMI TCP 坐标系

## 坐标系定义

双臂数据使用两个固定语义的 ROS 坐标系：

| 手别 | `teleop_role` | TCP frame | Jetson NX / Lite 位姿源 | Lite 779 位姿源 |
| --- | --- | --- | --- | --- |
| 右手 | `right_hand` | `right_tcp` | `insight3_a` | `insight7_a` |
| 左手 | `left_hand` | `left_tcp` | `insight3_b` | `insight7_b` |

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

Jetson NX 当前实测的左右外参一致：

```text
translation_m = [0.00, 0.08, 0.18]
rotation_xyzw = [0.00, 0.00, 0.00, 1.00]
```

即 TCP 相对 `camera_center` 向下 8 cm、向前 18 cm，旋转为 identity。
Insight3 global localizer 发布以下静态 TF：

```text
insight3_a_global_camera_center -> right_tcp
insight3_b_global_camera_center -> left_tcp
```

其他设备 profile 仍需单独实测，不复用 Jetson NX 或 UMI GoPro 的机械尺寸。

## 数据约定

- 位置单位为米，四元数顺序为 `xyzw`。
- `tcp_pose` 表示 `T_map_tcp`，而不是 `T_tcp_map`。
- 后续 20 Hz 双臂数据固定按右手、左手排列；手别由 `teleop_role` 决定，
  不依赖设备名的字母后缀。
- 设备 profile 在 `config/devices/<profile>/cameras.json` 中用 `tcp_frame_id`
  显式记录相机与 TCP frame 的对应关系。
