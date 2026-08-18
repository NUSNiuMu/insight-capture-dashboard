#!/usr/bin/env python3
"""Compatibility entry point for the host voice service."""

from _bootstrap import PROJECT_ROOT

from insight_capture.voice.service import *  # noqa: F401,F403
from insight_capture.voice.service import main


if __name__ == "__main__":
    raise SystemExit(main())
