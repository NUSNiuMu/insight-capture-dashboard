const roles = { head: "Head", right_hand: "Wrist A", left_hand: "Wrist B" };

export function deviceLabel(device) {
  return `${roles[device.role]} · ${device.name.split("_")[0].replace("insight", "Insight").replace("egohalo", "EgoHalo")}`;
}

export function cameraLabel(name) {
  return name.replace("insight9_a", "Insight9 · RGB").replace("insight3_a", "Wrist A").replace("insight3_b", "Wrist B")
    .replace("egohalo_side_left", "EgoHalo · Left side").replace("egohalo_stereo_left", "EgoHalo · Front left")
    .replace("egohalo_stereo_right", "EgoHalo · Front right").replace("egohalo_side_right", "EgoHalo · Right side");
}

export function issueText(issue) {
  const d = issue.details || {};
  const checks = {
    task_inactive: "Choose a task before recording.",
    camera_count: "Connect one head device and two wrist devices.",
    camera_inputs_missing: `${cameraLabel(d.camera || "Device")}: missing ${(d.missing || []).join(" and ")} input.`,
    camera_stale: `${cameraLabel(d.camera || "Device")}: no recent video.`,
    pose_stale: `${cameraLabel(d.camera || "Device")}: no recent pose.`,
    mapping_not_ready: "Head mapping is not ready.",
    localization_not_ready: `${cameraLabel(d.camera || "Device")}: localization is not ready.`,
    storage_unwritable: "Cannot write to the recording disk. Check its connection and permissions.",
    storage_fallback: "Capture disk unavailable. Recordings will use the backup disk.",
    storage_low: "Not enough free space on the recording disk.",
    topic_discovery_failed: "Cannot discover device data. Check the capture service.",
    recorder_topics_missing: `${(d.topics || []).length} required data streams are unavailable.`,
    distributed_not_ready: "Distributed recording is not ready. Check device connections and time sync.",
  };
  return checks[issue.code] || issue.message;
}

export function errorText(message) {
  const messages = {
    recording_busy: "Wait for recording or saving to finish before calibrating.",
    calibration_cameras_not_ready: "Calibration needs video and VIO from the head device.",
    calibration_service_not_ready: "Mapping service is not ready.",
    "Failed to fetch": "Cannot reach Jetson. Check your Wi-Fi connection.",
    "请先结束录制或等待当前后处理完成": "Stop recording and wait for the current transfer to finish.",
    "请选择尚未合包的已保存录制": "Choose a saved recording that has not been merged.",
    "请连接保存该条数据的原 EgoHalo 设备": "Connect the EgoHalo that recorded this take.",
  };
  return messages[message] || message;
}
