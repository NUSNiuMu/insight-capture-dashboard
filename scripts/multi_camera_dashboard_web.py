#!/usr/bin/env python3
"""Compatibility entry point for the packaged capture runtime."""

from _bootstrap import PROJECT_ROOT

from insight_capture.runtime.app import main  # noqa: E402


if __name__ == "__main__":
    main()
