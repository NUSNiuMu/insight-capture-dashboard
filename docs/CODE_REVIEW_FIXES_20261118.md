# 后端与 Qt 普通代码审理修复记录

修复基线：`b2545e01153562bb23c80f836b68ba192bc358db`。本轮对应此前全量源码审理报告中的 Q01–Q23，仅处理普通功能、状态、资源管理和格式校验问题。此前审理范围为后端 120 个文件约 18,339 行、Qt 60 个文本文件约 6,240 行，以及相关配置；字体只核对来源。本轮没有恢复已停止的深度安全检查，也没有对未完成核实的安全线索作漏洞结论。

## 修复对应表

| ID | 修复后的行为 | 主要位置 |
| --- | --- | --- |
| Q01 | HTTP 停止请求取消后，停止线程与 Take 完成操作仍持有准入锁直到结束 | `api/routes/recording.py` |
| Q02 | 单条录制的 metadata 字段类型错误只使该条无效，不中断整个录制库枚举 | `core/bag.py` |
| Q03 | 自定义设备角色使用角色原名生成标签，手机状态不会因闭集字典查找失败 | `api/routes/mobile.py` |
| Q04 | 停止超时仍存活的写入进程及其控制 FD 保留管理权，阻止新录制与删除，允许再次停止 | `runtime/recording/manager.py` |
| Q05 | 录制子进程生成后的所有启动步骤共用异常收尾；标记写入等失败也会停止子进程，未退出者继续受管理 | `runtime/recording/manager.py` |
| Q06 | 起录回滚记录停止时间；完成合包状态读取使用可缺省的时间链，不提前访问缺失键 | `runtime/recording/distributed.py` |
| Q07 | Take 按严格数字文件名和数值编号排序，跨越 9999 后仍选取最新 Take | `runtime/take.py` |
| Q08 | 准入采用的全局定位要求保存到状态、Take、单端与双端元数据；原始采集不因缺少全局图误报，仍监控设备 VIO、图像、时钟与存储 | `api/routes/recording.py`、`runtime/anomaly.py`、录制管理器 |
| Q09 | QC 的 started/resolved 状态只在写盘成功后更新，短暂写入失败可重试 | `runtime/anomaly.py` |
| Q10 | 正常 ROS context/executor 关闭返回正常清理路径；真正执行器故障继续走原来的故障处理 | `runtime/app.py` |
| Q11 | 评分文件、评分徽标与完整性结果统一使用录制路径的稳定 ID，不同目录下同名录制各自保存 | `services/scoring.py`、`postprocess/bags/catalog.py`、录制 API |
| Q12 | 相机时钟/跟踪重置同时清除图像采样期限，避免沿用上一轮设备时间 | `runtime/mapping/looper_node.py` |
| Q13 | 动态 DDS 地址刷新保留初始配置的排除地址 | `runtime/ros/network.py` |
| Q14 | 选中的静态 TF 完全缺失时按静态主题存在性检查判失败 | `postprocess/bags/integrity.py` |
| Q15 | 非有限数值、空输入、长度不匹配和无效标定参数不能生成满分；评分纯计算可独立于 ROS 导入验证 | `postprocess/quality/trajectory_score.py` |
| Q16 | 无效回放 manifest 或 camera 条目被视为缓存失效，走正常重建路径 | `postprocess/bags/synchronization.py` |
| Q17 | 旧录制删除入口也经过管理器生命周期互斥，本地 finalizing/merging 与双端合包期间不能删除 | `api/routes/recording.py`、录制管理器 |
| Q18 | 回放加载忙时拒绝重复 Load，bag 身份贯穿 start、manifest 与 activate | `native_dashboard/dashboard.*` |
| Q19 | Live/退出使旧回放请求代次失效，迟到的成功或错误响应不能恢复旧回放或清理新请求状态 | `native_dashboard/dashboard.*` |
| Q20 | 视频先全部 PAUSED 预加载，再共同释放；仅主视频驱动姿态时间轴，从视频按主时间修正，跳转统一执行 | `native_dashboard/Main.qml`、`video.*` |
| Q21 | 原生视频连接时即启动监视定时器，尚未收到首帧、尚未创建 pipeline 也能超时重连 | `native_dashboard/video.cpp` |
| Q22 | 优化轨迹成功加载后才更新 loadedRun，失败保留原图及其正确标签，允许重试 | `pages/OptimizationPage.qml`、优化 API |
| Q23 | GLB 转换校验 accessor/view/image 索引、整数参数、步长、各自 view 范围及转换输出长度；使用受限的 64 位范围计算 | `native_dashboard/model.cpp` |

## 验证与边界

- 修复前新增普通回归测试捕获了 11 个失败；其余问题沿用既有审理的最小复现与源码调用链证据。
- 后端最终全量回归：**571 项通过、4 项跳过**。跳过项为 1 个 ROS 映射测试模块、2 个需显式启用的 Jetson 硬件测试、1 个仅支持 ARM64 的 go2rtc 测试。
- 后端回归使用现有依赖，在断网、只读源码、临时写目录及资源限额的沙箱中运行；没有启动现场服务或实际录制。
- `tests/runtime/test_review_regressions.py` 覆盖取消停止、进程收尾、目录与 Take、QC 写入、原始采集要求、DDS、评分、缓存和正常 ROS 关闭；删除入口测试使用真实本地录制管理器。
- `native_dashboard/tests/qml_behavior.cjs` 直接提取并执行生产 QML 的 JavaScript 处理器，验证预加载屏障、主从时间、轨迹加载失败与重试。该测试不等同于渲染和 GStreamer 验收。
- 两个修改后的 QML 文件通过已有 Qt5 `qmlformat` 语法检查。独立 `model-conversion` 正常模型测试使用已有 Qt5 Core/Gui 编译并运行成功；覆盖普通量化模型转换和无需转换的模型。
- 已新增 `playback-actions` Qt6 C++ 回归，覆盖重复 Load、返回 Live、迟到 activate 和随后加载另一条录制。本机 CMake 配置因缺少 `Qt6Config.cmake` 无法完成，该用例与完整 Qt6 客户端未运行/未构建。
- ROS 专用映射测试在缺少运行环境时跳过；相机采样期限重置有相应 ROS 回归用例。正常关闭异常的分支另有不依赖 ROS 运行时的单元测试。
- 未安装新依赖、构建发布包、部署、重启服务或进行相机/Jetson 端到端验收。23 项均有源码修复；不能据此宣称所有设备行为都已验收。

## 结果文件兼容性

新的评分与完整性报告写为 `bag-<稳定摘要>.json`。旧的纯 basename 报告无法可靠区分不同目录下的同名录制，因此不再用它们给录制库打徽标；重新运行对应评分/完整性检查即可生成新的结果。既有数据和旧报告未删除。

## 重复验证

在依赖已准备好的隔离环境中：

```bash
python3 -m pytest -q tests
node native_dashboard/tests/qml_behavior.cjs
cmake -S native_dashboard/tests -B /tmp/insight-native-tests
cmake --build /tmp/insight-native-tests
ctest --test-dir /tmp/insight-native-tests --output-on-failure
```

后三条要求已有 Qt6.2+、Quick3D、QuickControls2、Network 和 WebSockets；本轮没有为满足这些要求下载或安装依赖。
