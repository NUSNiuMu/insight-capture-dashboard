"""Composable runtime services for the dashboard ROS node."""

from .image_pipeline import ImagePipeline
from .models import CameraFrame, CameraSpec, PoseSpec
from .payloads import PayloadBuilder
from .recording_bridge import RecordingBridge
from .watchdog import ParticipantWatchdog
from .worker_supervisor import WorkerSupervisor

__all__ = [
    "CameraFrame",
    "CameraSpec",
    "ImagePipeline",
    "ParticipantWatchdog",
    "PayloadBuilder",
    "PoseSpec",
    "RecordingBridge",
    "WorkerSupervisor",
]
