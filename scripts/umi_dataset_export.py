#!/usr/bin/env python3
"""Compatibility entry point for the legacy UMI exporter."""

from _bootstrap import PROJECT_ROOT

from insight_capture.legacy.umi_zarr import *  # noqa: F401,F403
from insight_capture.legacy.umi_zarr import main


if __name__ == "__main__":
    raise SystemExit(main())
