#!/usr/bin/env python3
"""Thin field entry point for rosbag integrity analysis."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from insight_capture.postprocess.bags.integrity import *  # noqa: F401,F403
from insight_capture.postprocess.bags.integrity import main


if __name__ == "__main__":
    main()
