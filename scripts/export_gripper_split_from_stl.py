#!/usr/bin/env python3

"""Split the single fused vis_assembly.stl (exported from the real CAD
assembly, all parts still in shared assembly coordinates) into base/left/right
groups by connected mesh component, then export a GLB with the two finger
groups as independently-animatable nodes.

The CAD export could not isolate individual bodies, so this relies on the
fact that the finger parts are physically separate solids (touching, not
vertex-welded to the base) — trimesh.split() recovers them as distinct
connected components. Left/right finger sub-parts are identified as mirror
pairs (matching bounding-box size, centroid X negated, Y/Z equal); everything
without a mirror partner is treated as the static base.
"""

import argparse
from pathlib import Path
from typing import List, Tuple

import numpy as np
import trimesh


def find_mirror_pairs(parts: List[trimesh.Trimesh]) -> Tuple[List[Tuple[int, int]], List[int]]:
    used = set()
    pairs: List[Tuple[int, int]] = []
    for i, p in enumerate(parts):
        if i in used:
            continue
        best_j, best_score = None, None
        for j, q in enumerate(parts):
            if j <= i or j in used:
                continue
            size_diff = float(np.abs((p.bounds[1] - p.bounds[0]) - (q.bounds[1] - q.bounds[0])).max())
            cx_sum = abs(p.centroid[0] + q.centroid[0])
            cy_diff = abs(p.centroid[1] - q.centroid[1])
            cz_diff = abs(p.centroid[2] - q.centroid[2])
            if size_diff < 1e-4 and cx_sum < 1e-3 and cy_diff < 1e-3 and cz_diff < 1e-3 and abs(p.centroid[0]) > 5e-3:
                score = size_diff + cx_sum + cy_diff + cz_diff
                if best_score is None or score < best_score:
                    best_score = score
                    best_j = j
        if best_j is not None:
            used.add(i)
            used.add(best_j)
            pairs.append((i, best_j))
    singles = [i for i in range(len(parts)) if i not in used]
    return pairs, singles


def export_split_gripper(
    input_stl: Path,
    output_glb: Path,
    slide_axis: np.ndarray,
    max_travel_m: float,
    drop_rearmost_static: bool = False,
    drop_component_indices: tuple = (),
) -> None:
    mesh = trimesh.load(input_stl, force="mesh")
    parts = mesh.split(only_watertight=False)
    pairs, singles = find_mirror_pairs(list(parts))
    if not pairs:
        raise RuntimeError("No left/right mirror pairs found — cannot identify finger parts")

    left_indices: List[int] = []
    right_indices: List[int] = []
    for i, j in pairs:
        a, b = (i, j) if parts[i].centroid[0] < parts[j].centroid[0] else (j, i)
        left_indices.append(a)
        right_indices.append(b)

    if drop_rearmost_static:
        rearmost = min(singles, key=lambda i: parts[i].bounds[0][1])
        print(f"[filter] dropping static component {rearmost} (rearmost, bbox_size={parts[rearmost].bounds[1]-parts[rearmost].bounds[0]})")
        singles = [i for i in singles if i != rearmost]

    for idx in drop_component_indices:
        if idx in singles:
            print(f"[filter] dropping static component {idx} (bbox_size={parts[idx].bounds[1]-parts[idx].bounds[0]})")
            singles = [i for i in singles if i != idx]

    base_mesh = trimesh.util.concatenate([parts[i] for i in singles])
    left_mesh = trimesh.util.concatenate([parts[i] for i in left_indices])
    right_mesh = trimesh.util.concatenate([parts[i] for i in right_indices])

    # Re-express each finger group relative to its own centroid so the GLB
    # node's translation is a meaningful pivot rather than an arbitrary
    # assembly-frame offset; base stays in the shared assembly frame (origin).
    left_origin = left_mesh.centroid.copy()
    right_origin = right_mesh.centroid.copy()
    left_mesh.apply_translation(-left_origin)
    right_mesh.apply_translation(-right_origin)

    scene = trimesh.Scene()
    scene.add_geometry(base_mesh, node_name="base_link", parent_node_name="world")
    left_transform = np.eye(4)
    left_transform[:3, 3] = left_origin
    scene.graph.update(frame_to="left_finger_holder", frame_from="world", matrix=left_transform)
    scene.add_geometry(left_mesh, node_name="left_finger_holder_mesh", parent_node_name="left_finger_holder")
    right_transform = np.eye(4)
    right_transform[:3, 3] = right_origin
    scene.graph.update(frame_to="right_finger_holder", frame_from="world", matrix=right_transform)
    scene.add_geometry(right_mesh, node_name="right_finger_holder_mesh", parent_node_name="right_finger_holder")

    output_glb.parent.mkdir(parents=True, exist_ok=True)
    scene.export(str(output_glb), file_type="glb")

    print(f"[export] GLB -> {output_glb}")
    print(f"[summary] {len(singles)} base components, {len(pairs)} left/right mirror pairs")
    print(f"[summary] base bbox_size_m = {base_mesh.bounds[1] - base_mesh.bounds[0]}")
    print(f"[summary] left_origin={left_origin} right_origin={right_origin}")
    print(f"[summary] assumed slide axis (local) = {slide_axis}, placeholder max_travel_m={max_travel_m}")
    print("[summary] NOTE: max_travel_m is a placeholder — real stroke should come from live AprilTag calibration")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stl",
        default=str(Path(__file__).resolve().parent.parent / "assets" / "models" / "vis_assembly.stl"),
    )
    parser.add_argument(
        "--glb",
        default=str(Path(__file__).resolve().parent.parent / "assets" / "models" / "vis_assembly_split_preview2.glb"),
    )
    parser.add_argument("--max-travel-m", type=float, default=0.025, help="Placeholder per-side travel distance")
    parser.add_argument(
        "--drop-rearmost-static",
        action="store_true",
        help="Drop the static base component with the most negative Y bound (e.g. an unwanted GoPro/mount block)",
    )
    parser.add_argument(
        "--drop-component",
        type=int,
        action="append",
        default=[],
        help="Drop a specific static component index (from the split() ordering); repeatable",
    )
    args = parser.parse_args()
    export_split_gripper(
        input_stl=Path(args.stl).resolve(),
        output_glb=Path(args.glb).resolve(),
        slide_axis=np.array([1.0, 0.0, 0.0]),
        max_travel_m=args.max_travel_m,
        drop_rearmost_static=args.drop_rearmost_static,
        drop_component_indices=tuple(args.drop_component),
    )


if __name__ == "__main__":
    main()
