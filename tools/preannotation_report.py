#!/usr/bin/env python3
"""How close pre-annotation (or a second annotator) came to the saved labels.

    # every recording under a capture root: the model draft as it stood when a
    # person saved (segments_draft_reviewed.json) vs. what was saved
    python3 tools/preannotation_report.py outputs/aruco-capture

    # any two segment lists, e.g. two annotators on the same recording
    python3 tools/preannotation_report.py --compare a/segments.json b/segments.json

Segments are paired greedily by temporal IoU (>= --iou, default 0.5). Reported:
  matched    share of reference segments that have a partner
  label      among matched pairs, share with the same subtask (id when both have
             one, else text)
  boundary   mean |start diff| and |end diff| of matched pairs, seconds
  extra      candidate segments with no partner (to delete in review)
and, for drafts, accuracy split by the model's confidence so its calibration
shows: if low-confidence segments are not the ones corrected, sorting review by
confidence will not help.
"""

import argparse
import json
import sys
from pathlib import Path

REVIEWED = "segments_draft_reviewed.json"


def load(path):
    value = json.loads(Path(path).read_text())
    return value["segments"] if isinstance(value, dict) else value


def iou(a, b):
    inter = min(a["end_s"], b["end_s"]) - max(a["start_s"], b["start_s"])
    union = max(a["end_s"], b["end_s"]) - min(a["start_s"], b["start_s"])
    return max(inter, 0.0) / union if union > 0 else 0.0


def same_label(a, b):
    if a.get("subtask_id") and b.get("subtask_id"):
        return a["subtask_id"] == b["subtask_id"]
    return (a.get("subtask") or a.get("task") or "").strip().lower() == (
        b.get("subtask") or b.get("task") or ""
    ).strip().lower()


def pair(reference, candidate, threshold):
    scored = sorted(
        ((iou(r, c), i, j) for i, r in enumerate(reference) for j, c in enumerate(candidate)),
        reverse=True,
    )
    used_r, used_c, pairs = set(), set(), []
    for score, i, j in scored:
        if score < threshold:
            break
        if i in used_r or j in used_c:
            continue
        used_r.add(i)
        used_c.add(j)
        pairs.append((reference[i], candidate[j]))
    return pairs


def compare(reference, candidate, threshold):
    pairs = pair(reference, candidate, threshold)
    out = {
        "reference": len(reference),
        "candidate": len(candidate),
        "matched": len(pairs),
        "label_ok": sum(same_label(r, c) for r, c in pairs),
        "start_err": sum(abs(r["start_s"] - c["start_s"]) for r, c in pairs),
        "end_err": sum(abs(r["end_s"] - c["end_s"]) for r, c in pairs),
        "by_confidence": {},
    }
    paired = {id(c): r for r, c in pairs}
    for c in candidate:
        conf = c.get("confidence")
        band = "无" if conf is None else ("<0.5" if conf < 0.5 else "0.5–0.8" if conf < 0.8 else "≥0.8")
        r = paired.get(id(c))
        ok = r is not None and same_label(r, c)
        n, good = out["by_confidence"].get(band, (0, 0))
        out["by_confidence"][band] = (n + 1, good + ok)
    return out


def add(total, part):
    for key, value in part.items():
        if key == "by_confidence":
            for band, (n, good) in value.items():
                tn, tg = total[key].get(band, (0, 0))
                total[key][band] = (tn + n, tg + good)
        else:
            total[key] = total.get(key, 0) + value


def show(name, r):
    m = r["matched"]
    pct = lambda a, b: f"{100 * a / b:5.1f}%" if b else "   — "
    print(
        f"{name:<44} 参考 {r['reference']:4d}  候选 {r['candidate']:4d}  "
        f"匹配 {pct(m, r['reference'])}  标签一致 {pct(r['label_ok'], m)}  "
        f"起点误差 {r['start_err'] / m if m else 0:5.2f}s  终点误差 {r['end_err'] / m if m else 0:5.2f}s  "
        f"多余 {r['candidate'] - m:3d}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", nargs="?", help="capture root holding recording folders")
    parser.add_argument("--compare", nargs=2, metavar=("REFERENCE", "CANDIDATE"))
    parser.add_argument("--iou", type=float, default=0.5)
    args = parser.parse_args()
    if args.compare:
        show(Path(args.compare[1]).parent.name or "candidate", compare(load(args.compare[0]), load(args.compare[1]), args.iou))
        return
    if not args.root:
        parser.error("give a capture root or --compare")
    total = {"by_confidence": {}}
    sessions = 0
    for folder in sorted(Path(args.root).iterdir()):
        draft, saved = folder / REVIEWED, folder / "segments.json"
        if not (draft.is_file() and saved.is_file()):
            continue
        result = compare(load(saved), load(draft), args.iou)
        show(folder.name, result)
        add(total, result)
        sessions += 1
    if not sessions:
        sys.exit(f"{args.root}: no recording has both {REVIEWED} and segments.json "
                 "(the snapshot is written when 保存全部标注 runs after a model draft)")
    print("-" * 140)
    show(f"合计（{sessions} 条录制）", total)
    print("\n按模型 confidence 分档：候选片段数 / 匹配且标签一致")
    for band in ("<0.5", "0.5–0.8", "≥0.8", "无"):
        if band in total["by_confidence"]:
            n, good = total["by_confidence"][band]
            print(f"  {band:<8} {n:5d}  {100 * good / n:5.1f}%")


if __name__ == "__main__":
    main()
