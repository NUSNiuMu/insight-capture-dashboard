#!/usr/bin/env python3

"""Compatibility command for offline rosbag gripper extraction."""

from _bootstrap import PROJECT_ROOT

from insight_capture.postprocess.gripper.extraction import main


if __name__ == "__main__":
    raise SystemExit(main())
