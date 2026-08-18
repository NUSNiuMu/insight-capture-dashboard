#!/usr/bin/env python3
"""Canonical thin CLI for LeRobot dataset export."""

from _bootstrap import PROJECT_ROOT

from insight_capture.postprocess.datasets.lerobot import main


if __name__ == "__main__":
    raise SystemExit(main())
