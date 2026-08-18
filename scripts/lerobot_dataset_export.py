#!/usr/bin/env python3
"""Compatibility entry point for the canonical LeRobot exporter."""

from _bootstrap import PROJECT_ROOT

from insight_capture.postprocess.datasets.lerobot import *  # noqa: F401,F403
from insight_capture.postprocess.datasets.lerobot import main


if __name__ == "__main__":
    raise SystemExit(main())
