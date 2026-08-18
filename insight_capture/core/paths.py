"""Repository paths shared by runtime and maintenance entry points."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / "config"
WEB_DIST_ROOT = PROJECT_ROOT / "web_dashboard" / "dist"


def runtime_config_path() -> Path:
    canonical = CONFIG_ROOT / "runtime.json"
    legacy = CONFIG_ROOT / "post_processing.json"
    return canonical if canonical.is_file() else legacy
