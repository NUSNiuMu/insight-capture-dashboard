from pathlib import Path
from types import SimpleNamespace

from insight_capture.core.config import build_dashboard_config, load_setup
from insight_capture.media.preview_config import preview_description


def profile_preview(profile):
    root = Path(__file__).resolve().parents[2]
    config = build_dashboard_config(load_setup(root / "config" / "devices" / profile / "cameras.json"))
    return preview_description(
        [SimpleNamespace(**camera) for camera in config["cameras"]],
        [SimpleNamespace(**pose) for pose in config["poses"]],
    )


def test_egohalo_overview_uses_four_ordered_head_views_and_keeps_wrist_sources():
    preview = profile_preview("jetson-nx-egohalo")
    overview = preview["overview"]

    assert preview["default_source"] == overview["id"] == "__overview__"
    assert overview["label"] == "四路总览"
    assert (overview["width"], overview["height"]) == (960, 1280)
    assert [(r["id"], r["x"], r["y"], r["width"], r["height"]) for r in overview["regions"]] == [
        ("egohalo_side_left", 0, 0, 480, 640),
        ("egohalo_stereo_left", 480, 0, 480, 640),
        ("egohalo_stereo_right", 0, 640, 480, 640),
        ("egohalo_side_right", 480, 640, 480, 640),
    ]
    assert [(s["id"], s["label"]) for s in preview["sources"]] == [
        ("egohalo_side_left", "左前"),
        ("egohalo_stereo_left", "前置左"),
        ("egohalo_stereo_right", "前置右"),
        ("egohalo_side_right", "右前"),
        ("insight3_b", "左腕左目"),
        ("insight3_a", "右腕左目"),
    ]


def test_insight9_preview_keeps_head_and_wrists_in_three_view_overview():
    preview = profile_preview("jetson-nx")

    assert preview["default_source"] == "__overview__"
    assert preview["overview"]["label"] == "三路总览"
    assert [region["id"] for region in preview["overview"]["regions"]] == [
        "insight9_a", "insight3_b", "insight3_a",
    ]
