"""aiohttp application factory and route table."""

from aiohttp import web

from .context import DashboardContext
from .middleware import create_json_error_middleware
from .routes.alignment import AlignmentRoutes
from .routes.cameras import CameraRoutes
from .routes.optimization import OptimizationRoutes
from .routes.playback import PlaybackRoutes
from .routes.recording import RecordingRoutes
from .routes.settings import SettingsRoutes
from .routes.static import StaticRoutes
from .websocket import PoseWebSocketService


def create_app(context: DashboardContext) -> web.Application:
    app = web.Application(middlewares=[create_json_error_middleware(context)])
    websocket = PoseWebSocketService(context)
    alignment = AlignmentRoutes(context)
    cameras = CameraRoutes(context)
    recording = RecordingRoutes(context)
    playback = PlaybackRoutes(context)
    optimization = OptimizationRoutes(context)
    settings = SettingsRoutes(context)
    static = StaticRoutes(context)

    async def handle_health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "fake_pose": context.node.fake_pose})

    app.router.add_get("/ws", websocket._handle_ws)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/api/alignment", alignment._handle_alignment_snapshot)
    app.router.add_post("/api/alignment/start", alignment._handle_alignment_start)
    app.router.add_post("/api/alignment/stop", alignment._handle_alignment_stop)
    app.router.add_get("/api/cameras", cameras._handle_camera_snapshot)
    app.router.add_get("/api/cameras/{camera_name}/frame", cameras._handle_camera_frame)
    app.router.add_get("/api/cameras/{camera_name}/hand", cameras._handle_camera_hand_overlay)
    app.router.add_get("/api/images/capabilities", cameras._handle_image_capabilities)
    app.router.add_get("/api/recording/status", recording._handle_recording_status)
    app.router.add_get("/api/recording/topics", recording._handle_recording_topics)
    app.router.add_post("/api/recording/start", recording._handle_recording_start)
    app.router.add_post("/api/recording/stop", recording._handle_recording_stop)
    app.router.add_post("/api/recording/sync", recording._handle_recording_sync)
    app.router.add_get("/api/rosbags", recording._handle_rosbag_list)
    app.router.add_delete("/api/rosbags/{bag_name}", recording._handle_rosbag_delete)
    app.router.add_post("/api/integrity/run", recording._handle_integrity_run)
    app.router.add_post("/api/scoring/run", recording._handle_scoring_run)
    app.router.add_get("/api/scoring/status", recording._handle_scoring_status)
    app.router.add_post("/api/playback/start", playback._handle_playback_start)
    app.router.add_post("/api/playback/stop", playback._handle_playback_stop)
    app.router.add_get("/api/playback/status", playback._handle_playback_status)
    app.router.add_post("/api/trajectory/clear", playback._handle_trajectory_clear)
    app.router.add_post("/api/optimization/start", optimization._handle_optimization_start)
    app.router.add_post("/api/optimization/stop", optimization._handle_optimization_stop)
    app.router.add_get("/api/optimization/status", optimization._handle_optimization_status)
    app.router.add_get("/api/optimization/trajectories", optimization._handle_optimization_trajectories)
    app.router.add_get("/api/optimization/runs", optimization._handle_optimization_runs)
    app.router.add_get("/api/settings", settings._handle_settings_get)
    app.router.add_post("/api/settings/avatar-model", settings._handle_settings_avatar_model)
    app.router.add_post("/api/settings/gripper-tracking", settings._handle_settings_gripper_tracking)
    app.router.add_get("/api/settings/board-calibration", settings._handle_settings_board_calibration_get)
    app.router.add_post("/api/settings/board-calibration", settings._handle_settings_board_calibration_post)
    app.router.add_get("/api/settings/rosbag-sync", settings._handle_settings_rosbag_sync_get)
    app.router.add_post("/api/settings/rosbag-sync", settings._handle_settings_rosbag_sync_post)
    app.router.add_post("/api/settings/restart-backend", settings._handle_settings_restart)
    app.router.add_post("/api/settings/hand-overlay", settings._handle_settings_hand_overlay)
    app.router.add_post("/api/settings/stick-figure", settings._handle_settings_stick_figure)
    app.router.add_get("/asset", static._handle_asset)

    if context.web_root and context.web_root.exists():
        app.router.add_get("/", static._handle_index)
        app.router.add_get("/3d", static._handle_index)
        app.router.add_get("/images", static._handle_images_page)
        app.router.add_get("/bags", static._handle_bags_page)
        app.router.add_get("/recording", static._handle_recording_page)
        app.router.add_get("/scoring", static._handle_scoring_page)
        app.router.add_get("/optimization", static._handle_optimization_page)
        app.router.add_get("/settings", static._handle_settings_page)
        static_root = context.web_root / "static"
        if static_root.exists():
            app.router.add_static("/static/", str(static_root), show_index=False)
        runs_root = context.project_root / "runs"
        runs_root.mkdir(exist_ok=True)
        app.router.add_static("/optimization-runs/", str(runs_root), show_index=False)

    app.on_startup.append(websocket._on_startup)
    app.on_shutdown.append(websocket._on_shutdown)
    return app
