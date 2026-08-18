#!/usr/bin/env python3
"""Canonical thin CLI for LeRobot dataset export."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from insight_capture.postprocess.datasets.lerobot import main


if __name__ == "__main__":
    raise SystemExit(main())
