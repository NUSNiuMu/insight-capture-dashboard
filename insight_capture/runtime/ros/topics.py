"""ROS topic mappings shared by live and playback adapters."""


def playback_topic(topic: str) -> str:
    """Return the isolated playback topic for a live ROS topic."""

    return f"/bagplay{topic}"
