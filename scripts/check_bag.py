#!/usr/bin/env python3
"""Compatibility command for rosbag integrity analysis."""

from _bootstrap import PROJECT_ROOT

from insight_capture.postprocess.bags.integrity import *  # noqa: F401,F403


if __name__ == "__main__":
    main()
