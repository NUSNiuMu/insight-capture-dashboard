#!/usr/bin/env python3
"""Thin field entry point for synchronized camera restart diagnostics."""

from pathlib import Path
import runpy


runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "tools/diagnostics/sync_camera_restart.py"),
    run_name="__main__",
)
