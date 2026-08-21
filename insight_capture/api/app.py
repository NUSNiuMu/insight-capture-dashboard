"""aiohttp application factory and route table."""

from aiohttp import web

from .context import DashboardContext
from .middleware import create_json_error_middleware, create_static_cache_middleware
from .routes.cameras import CameraRoutes
from .routes.quality import CaptureCheckRoutes
from .routes.gripper import GripperExtractionRoutes
from .routes.handpose import HandPoseRoutes
from .routes.mapping import MappingRoutes
from .routes.optimization import OptimizationRoutes
from .routes.playback import PlaybackRoutes
from .routes.recording import RecordingRoutes
from .routes.runtime import RuntimeRoutes
from .routes.sessions import SessionRoutes
from .routes.settings import SettingsRoutes
from .routes.static import StaticRoutes
from .routes.datasets import UmiExportRoutes
from .routes.takes import TakeRoutes
from .routes.tasks import TaskRoutes
from .websocket import PoseWebSocketService


def create_app(context: DashboardContext) -> web.Application:
    app = web.Application(
        middlewares=[
            create_json_error_middleware(context),
            create_static_cache_middleware(),
        ]
    )
    websocket = PoseWebSocketService(context)
    cameras = CameraRoutes(context)
    capture_check = CaptureCheckRoutes(context)
    gripper = GripperExtractionRoutes(context)
    handpose = HandPoseRoutes(context)
    mapping = MappingRoutes(context)
    recording = RecordingRoutes(context)
    runtime = RuntimeRoutes(context)
    sessions = SessionRoutes(context)
    takes = TakeRoutes(context)
    tasks = TaskRoutes(context)
    playback = PlaybackRoutes(context)
    optimization = OptimizationRoutes(context)
    settings = SettingsRoutes(context)
    static = StaticRoutes(context)
    umi_export = UmiExportRoutes(context)

    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "fake_pose": context.node.fake_pose})

    app.router.add_get("/ws", websocket._handle_ws)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/cameras", cameras._handle_camera_snapshot)
    app.router.add_post(
        "/api/cameras/{camera_name}/browser-stats",
        cameras._handle_browser_stats,
    )
    app.router.add_get("/api/mapping", mapping._handle_snapshot)
    app.router.add_post("/api/mapping/reset", mapping._handle_reset)
    app.router.add_get("/api/capture-check", capture_check._handle_status)
    app.router.add_post("/api/capture-check/run", capture_check._handle_run)
    app.router.add_get("/api/cameras/{camera_name}/frame", cameras._handle_camera_frame)
    app.router.add_get("/api/images/capabilities", cameras._handle_image_capabilities)
    app.router.add_get("/api/recording/status", recording._handle_recording_status)
    app.router.add_get("/api/preflight", runtime._handle_preflight)
    app.router.add_get("/api/system/status", runtime._handle_system_status)
    app.router.add_post("/api/system/status", runtime._handle_system_status)
    app.router.add_get("/api/sessions", sessions._handle_list)
    app.router.add_get("/api/tasks", tasks._handle_list)
    app.router.add_get("/api/tasks/current", tasks._handle_current)
    app.router.add_post("/api/tasks/current", tasks._handle_current)
    app.router.add_post("/api/tasks/current/end", tasks._handle_end)
    app.router.add_post("/api/tasks/{task_id}/activate", tasks._handle_activate)
    app.router.add_post("/api/takes/current/reject", takes._handle_reject_current)
    app.router.add_get("/api/voice/alerts", runtime._handle_voice_alerts)
    app.router.add_get("/api/recording/topics", recording._handle_recording_topics)
    app.router.add_get(
        "/api/recording/storage/directories",
        recording._handle_recording_directories,
    )
    app.router.add_post(
        "/api/recording/storage/select",
        recording._handle_recording_directory_select,
    )
    app.router.add_post("/api/recording/start", recording._handle_recording_start)
    app.router.add_post("/api/recording/stop", recording._handle_recording_stop)
    app.router.add_post(
        "/api/automation/recording/start",
        recording._handle_automation_recording_start,
    )
    app.router.add_post(
        "/api/automation/recording/vio-calibration/start",
        recording._handle_automation_vio_calibration_start,
    )
    app.router.add_post(
        "/api/automation/recording/stop",
        recording._handle_automation_recording_stop,
    )
    app.router.add_get("/api/rosbags", recording._handle_rosbag_list)
    app.router.add_post("/api/gripper-extraction/start", gripper._handle_start)
    app.router.add_get("/api/gripper-extraction/status", gripper._handle_status)
    app.router.add_get("/api/gripper-extraction/result", gripper._handle_result)
    app.router.add_post("/api/umi-export/start", umi_export._handle_start)
    app.router.add_get("/api/umi-export/status", umi_export._handle_status)
    app.router.add_get("/api/handpose/capabilities", handpose._handle_capabilities)
    app.router.add_get("/api/handpose/status", handpose._handle_status)
    app.router.add_post("/api/handpose/start", handpose._handle_start)
    app.router.add_post("/api/handpose/stop", handpose._handle_stop)
    app.router.add_get("/api/handpose/result", handpose._handle_result)
    app.router.add_get("/api/handpose/preview", handpose._handle_preview)
    app.router.add_delete("/api/rosbags/{bag_name}", recording._handle_rosbag_delete)
    app.router.add_post("/api/integrity/run", recording._handle_integrity_run)
    app.router.add_post("/api/scoring/run", recording._handle_scoring_run)
    app.router.add_get("/api/scoring/status", recording._handle_scoring_status)
    app.router.add_post("/api/playback/start", playback._handle_playback_start)
    app.router.add_post("/api/playback/prebuild", playback._handle_playback_prebuild)
    app.router.add_post("/api/playback/activate", playback._handle_playback_activate)
    app.router.add_post("/api/playback/stop", playback._handle_playback_stop)
    app.router.add_get("/api/playback/status", playback._handle_playback_status)
    app.router.add_post(
        "/api/playback/browser-stats",
        playback._handle_playback_browser_stats,
    )
    app.router.add_get(
        "/api/playback/artifacts/{bag_name}/{filename}",
        playback._handle_playback_artifact,
    )
    app.router.add_post("/api/trajectory/clear", playback._handle_trajectory_clear)
    app.router.add_post("/api/optimization/start", optimization._handle_optimization_start)
    app.router.add_post("/api/optimization/stop", optimization._handle_optimization_stop)
    app.router.add_get("/api/optimization/status", optimization._handle_optimization_status)
    app.router.add_get("/api/optimization/trajectories", optimization._handle_optimization_trajectories)
    app.router.add_get("/api/optimization/runs", optimization._handle_optimization_runs)
    app.router.add_get("/api/settings", settings._handle_settings_get)
    app.router.add_post("/api/settings/gripper-tracking", settings._handle_settings_gripper_tracking)
    app.router.add_post(
        "/api/settings/insight3-gripper-mask",
        settings._handle_settings_insight3_gripper_mask,
    )
    app.router.add_post("/api/settings/restart-backend", settings._handle_settings_restart)
    app.router.add_post("/api/settings/hand-overlay", settings._handle_settings_hand_overlay)
    app.router.add_post(
        "/api/settings/voice-volume",
        settings._handle_settings_voice_volume,
    )
    app.router.add_post(
        "/api/settings/voice-sample",
        settings._handle_settings_voice_sample,
    )
    app.router.add_get("/asset", static._handle_asset)

    if context.web_root and context.web_root.exists():
        app.router.add_get("/", static._handle_index)
        app.router.add_get("/sessions", static._handle_index)
        app.router.add_get("/3d", static._handle_spatial_page)
        app.router.add_get("/images", static._handle_images_page)
        app.router.add_get("/bags", static._handle_bags_page)
        app.router.add_get("/umi-dataset", static._handle_umi_dataset_page)
        app.router.add_get("/recording", static._handle_recording_page)
        app.router.add_get("/scoring", static._handle_scoring_page)
        app.router.add_get("/optimization", static._handle_optimization_page)
        app.router.add_get("/handpose", static._handle_handpose_page)
        app.router.add_get("/settings", static._handle_settings_page)
        static_root = context.web_root / "static"
        if static_root.exists():
            app.router.add_static("/static/", str(static_root), show_index=False)
        runs_root = context.project_root / "runs"
        runs_root.mkdir(exist_ok=True)
        app.router.add_static("/optimization-runs/", str(runs_root), show_index=False)

    app.on_startup.append(websocket._on_startup)
    app.on_shutdown.append(gripper._on_shutdown)
    app.on_shutdown.append(umi_export._on_shutdown)
    app.on_shutdown.append(handpose._on_shutdown)
    app.on_shutdown.append(playback._on_shutdown)
    app.on_shutdown.append(websocket._on_shutdown)
    return app
