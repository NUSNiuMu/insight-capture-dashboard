"""Compatibility CLI for gripper calibration in the perception layer."""

from insight_capture.perception.gripper.calibration import *  # noqa: F401,F403
from insight_capture.perception.gripper.calibration import main


if __name__ == "__main__":
    main()
