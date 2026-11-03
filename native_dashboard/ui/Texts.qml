pragma Singleton
import QtQuick

QtObject {
    readonly property var names: ({
        "idle": "待命", "pending": "待处理", "preparing": "准备中", "starting": "启动中",
        "running": "进行中", "recording": "录制中", "saving": "保存中", "saved": "已保存",
        "accepted": "已接受", "rejected": "已作废", "operator_rejected": "操作员作废",
        "done": "已完成", "ready": "已就绪", "playing": "回放中", "paused": "已暂停",
        "stopped": "已停止", "stopping": "停止中", "cancelled": "已取消", "failed": "失败",
        "error": "失败", "warning": "警告", "pass": "通过", "suspect": "待复核",
        "good": "良好", "excellent": "优秀", "fair": "一般", "poor": "较差", "bad": "不佳",
        "yes": "是", "no": "否", "unknown": "未知", "unavailable": "不可用",
        "copying": "回传中", "merging": "合包中", "finalizing": "收尾中",
        "head": "头部", "right_hand": "腕部甲", "left_hand": "腕部乙",
        "dual_arm_umi": "双臂采集", "capture": "常规采集", "vio_calibration": "里程计校准",
        "extracting": "提取图像", "matching": "匹配特征", "reconstructing": "重建中",
        "aligning": "轨迹对齐", "complete": "已完成", "success": "成功",
        "extracting vio": "提取里程计", "extracting color images": "提取彩色图像",
        "running colmap": "图像重建", "aligning trajectories (sim3)": "轨迹相似变换对齐",
        "colmap": "图像重建", "pulse": "系统音频", "alsa": "设备音频"
    })
    function value(raw) {
        var text = raw === undefined || raw === null ? "" : String(raw);
        if (text.indexOf("voice service is unavailable") === 0) return "离线语音服务未启动，请检查语音服务和模型";
        return names[text.toLowerCase()] || text.replace(/Infrared/g, "红外").replace(/Color/g, "彩色");
    }
}
