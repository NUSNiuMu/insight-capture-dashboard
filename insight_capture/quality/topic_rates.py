"""Expected fleet topic rates shared by live and post-capture quality checks."""

from typing import Optional


def nominal_for(topic: str) -> Optional[float]:
    """Return the measured/configured rate for continuous fleet topics."""

    if topic.endswith("/imu"):
        return 400.0
    if topic.endswith("/vio_100hz"):
        # Insight3 B is a measured 97 Hz source even without a recorder.
        return 97.0 if topic.startswith("/insight3_b/") else 99.0
    if topic.startswith(("/insight_global/", "/insight9_sparse_map/")):
        # Mapping/localization output is conditional: pose/path streams can be
        # silent until a map exists or localization succeeds.
        return None
    if "/camera/" not in topic:
        return None
    if topic.startswith("/insight3_"):
        if "/depth/" in topic:
            # Insight3 B firmware 2.1.3 emits depth at 13 Hz (65-96 ms
            # intervals); it is not synchronized to the 30 Hz IR streams.
            return 13.0
        if any(
            fragment in topic
            for fragment in (
                "/image_raw",
                "/image_rect_raw",
                "camera_info",
                "vio_image",
            )
        ):
            return 30.0
    if topic.startswith("/insight9_"):
        if "/depth/" in topic:
            # Measured Insight9 depth cadence is approximately 12.85 Hz even
            # though its infrared and VIO image streams run at 20 Hz.
            return 12.85
        if "/color/" in topic:
            return (
                30.0
                if any(
                    fragment in topic
                    for fragment in (
                        "/image_raw",
                        "/image_rect_raw",
                        "camera_info",
                    )
                )
                else None
            )
        if any(
            fragment in topic
            for fragment in (
                "/image_raw",
                "/image_rect_raw",
                "camera_info",
                "vio_image",
            )
        ):
            return 20.0
    return None
