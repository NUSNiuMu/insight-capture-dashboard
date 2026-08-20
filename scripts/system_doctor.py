#!/usr/bin/env python3
"""Thin field entry point for the comprehensive system doctor."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from insight_capture.diagnostics.system_doctor import main


if __name__ == "__main__":
    raise SystemExit(main())
