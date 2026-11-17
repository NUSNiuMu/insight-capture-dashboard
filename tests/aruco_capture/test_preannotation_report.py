import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "preannotation_report", Path(__file__).parents[2] / "tools" / "preannotation_report.py"
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def seg(a, b, sub, conf=None):
    return {"start_s": a, "end_s": b, "subtask_id": sub, "subtask": sub, "confidence": conf}


def test_pairs_by_iou_and_scores_labels_boundaries_and_confidence():
    saved = [seg(0, 2, "pick"), seg(2, 4, "place"), seg(4, 6, "push")]
    draft = [seg(0, 2.2, "pick", 0.9), seg(2.2, 4, "pick", 0.3), seg(7, 8, "wave", 0.7)]
    r = report.compare(saved, draft, 0.5)
    assert (r["matched"], r["label_ok"]) == (2, 1)
    assert abs(r["start_err"] - 0.2) < 1e-9 and abs(r["end_err"] - 0.2) < 1e-9
    assert r["by_confidence"] == {"≥0.8": (1, 1), "<0.5": (1, 0), "0.5–0.8": (1, 0)}
