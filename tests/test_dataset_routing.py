from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from dataset_routing import select_lerobot_route  # noqa: E402


def test_any_repeated_dual_marker_detection_selects_umi() -> None:
    assert select_lerobot_route(
        {
            "insight3_a": {"dual_marker_hits": 2},
            "insight3_b": {"dual_marker_hits": 0},
        }
    ) == "umi_gripper"


def test_no_dual_marker_detection_selects_hand_inference() -> None:
    assert select_lerobot_route(
        {
            "insight3_a": {"dual_marker_hits": 0},
            "insight3_b": {"dual_marker_hits": 1},
        }
    ) == "ego_hand"
