#!/usr/bin/env python3
"""Compatibility facade for device and camera configuration."""

from _bootstrap import PROJECT_ROOT

from insight_capture.common.config import *  # noqa: F401,F403
from insight_capture.common.config import main


if __name__ == "__main__":
    main()
