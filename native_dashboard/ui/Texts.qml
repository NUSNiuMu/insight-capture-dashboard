pragma Singleton
import QtQuick

QtObject {
    readonly property var names: ({
        "idle": "Idle", "pending": "Pending", "preparing": "Preparing", "starting": "Starting",
        "running": "Running", "recording": "Recording", "saving": "Saving", "saved": "Saved",
        "accepted": "Accepted", "rejected": "Rejected", "operator_rejected": "Rejected by operator",
        "done": "Done", "ready": "Ready", "playing": "Playing", "paused": "Paused",
        "stopped": "Stopped", "stopping": "Stopping", "cancelled": "Cancelled", "failed": "Failed",
        "error": "Error", "warning": "Warning", "pass": "Pass", "suspect": "Review",
        "good": "Good", "excellent": "Excellent", "fair": "Fair", "poor": "Poor", "bad": "Bad",
        "yes": "Yes", "no": "No", "unknown": "Unknown", "unavailable": "Unavailable",
        "copying": "Retrieving", "merging": "Merging", "finalizing": "Finalizing",
        "head": "Head", "right_hand": "Wrist A", "left_hand": "Wrist B",
        "dual_arm_umi": "Dual-arm capture", "capture": "Capture", "vio_calibration": "VIO calibration",
        "extracting": "Extracting images", "matching": "Matching features", "reconstructing": "Reconstructing",
        "aligning": "Aligning trajectories", "complete": "Done", "success": "Success",
        "extracting vio": "Extracting VIO", "extracting color images": "Extracting color images",
        "running colmap": "Reconstruction", "aligning trajectories (sim3)": "Aligning trajectories (Sim3)",
        "recording_busy": "Stop recording and wait for saving to finish.",
        "calibration_cameras_not_ready": "Calibration needs video and VIO from all devices.",
        "calibration_service_not_ready": "Mapping service is not ready.",
        "请先结束录制或等待当前后处理完成": "Stop recording and wait for processing to finish.",
        "请选择尚未合包的已保存录制": "Choose a saved recording to merge.",
        "请连接保存该条数据的原 EgoHalo 设备": "Connect the EgoHalo that recorded this take.",
        "colmap": "Reconstruction", "pulse": "System audio", "alsa": "Device audio"
    })
    function value(raw) {
        var text = raw === undefined || raw === null ? "" : String(raw);
        if (text.indexOf("voice service is unavailable") === 0) return "Voice service unavailable. Check the service and models.";
        return names[text.toLowerCase()] || text.replace(/Infrared/g, "IR");
    }
}
