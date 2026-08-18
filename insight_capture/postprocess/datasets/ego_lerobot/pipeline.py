"""Cached end-to-end orchestration for Ego LeRobot deliveries."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
from pathlib import Path
import shutil
import tempfile
import time

from .annotations import apply_annotations
from .audit import audit_dataset
from .cache import bag_fingerprint, canonical_hash, materialize_tree, read_manifest, write_manifest
from .convert import convert_bag
from .model import load_backend
from .overlay import write_overlay
from .spec import load_spec


PIPELINE_VERSION = 3


@dataclass(frozen=True)
class ExportOptions:
    bag: Path
    output: Path
    spec_path: Path
    camera_config: Path
    cache_root: Path
    hand_backend: str = "wilor"
    model_dir: Path | None = None
    hand_confidence: float = 0.3
    max_image_skew_ms: float = 25.0
    max_pose_bracket_ms: float = 100.0
    projection_gate_px: float = 600.0
    temporal_same_step_m: float = 0.15
    temporal_advantage_m: float = 0.08
    reuse_dataset: Path | None = None
    reuse_overlay: Path | None = None
    write_review_overlay: bool = True
    decode_audit: bool = False


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base_cache_payload(options: ExportOptions, spec) -> dict[str, object]:
    model_files = {}
    model_dir = options.model_dir
    if options.hand_backend == "wilor" and model_dir is None:
        from .wilor_backend import DEFAULT_MODEL_DIR

        model_dir = DEFAULT_MODEL_DIR
    if model_dir and model_dir.is_dir():
        for path in sorted(model_dir.rglob("*")):
            if path.is_file():
                stat = path.stat()
                model_files[str(path.relative_to(model_dir))] = [stat.st_size, stat.st_mtime_ns]
    return {
        "pipeline_version": PIPELINE_VERSION,
        "bag": bag_fingerprint(options.bag),
        "crop": {"start_s": spec.crop_start_s, "end_s": spec.crop_end_s, "fps": spec.fps},
        "camera_config_sha256": _sha(options.camera_config),
        "hand_backend": options.hand_backend,
        "model_dir": str(model_dir.resolve()) if model_dir else None,
        "model_files": model_files,
        "hand_confidence": options.hand_confidence,
        "max_image_skew_ms": options.max_image_skew_ms,
        "max_pose_bracket_ms": options.max_pose_bracket_ms,
        "projection_gate_px": options.projection_gate_px,
        "temporal_same_step_m": options.temporal_same_step_m,
        "temporal_advantage_m": options.temporal_advantage_m,
    }


def _cache_payload(options: ExportOptions, spec) -> dict[str, object]:
    return {
        "base": _base_cache_payload(options, spec),
        "spec_sha256": _sha(options.spec_path),
    }


def _copy_reference(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(target)
    for item in source.rglob("*"):
        destination = target / item.relative_to(source)
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif item.suffix.lower() == ".mp4":
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                destination.hardlink_to(item)
            except OSError:
                shutil.copy2(item, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


def export_dataset(options: ExportOptions) -> dict[str, object]:
    """Build or materialize a complete delivery and return its audit summary."""

    spec = load_spec(options.spec_path)
    base_payload = _base_cache_payload(options, spec)
    base_key = canonical_hash(base_payload)
    base_entry = options.cache_root / "_stages/base" / base_key
    base_dataset = base_entry / "dataset"
    payload = _cache_payload(options, spec)
    cache_key = canonical_hash(payload)
    entry = options.cache_root / cache_key
    cached_dataset = entry / "dataset"
    cached_overlay = entry / "overlay.mp4"
    options.cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = options.cache_root / f"{cache_key}.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = read_manifest(entry / "cache.json")
        cache_hit = bool(manifest and manifest.get("complete") and cached_dataset.is_dir())
        if cache_hit:
            audit_dataset(cached_dataset, spec)
        else:
            if entry.exists():
                shutil.rmtree(entry)
            entry.mkdir(parents=True)
            with tempfile.TemporaryDirectory(prefix="ego_build_", dir=options.cache_root) as temporary:
                staged = Path(temporary) / "dataset"
                base_manifest = read_manifest(base_entry / "cache.json")
                if base_manifest and base_manifest.get("complete") and base_dataset.is_dir():
                    _copy_reference(base_dataset, staged)
                    apply_annotations(staged, spec)
                    build_mode = "cached_base_reannotation"
                elif options.reuse_dataset is not None:
                    audit_dataset(options.reuse_dataset, spec, decode_videos=options.decode_audit)
                    _copy_reference(options.reuse_dataset, staged)
                    build_mode = "validated_reference_import"
                else:
                    backend = load_backend(
                        options.hand_backend,
                        model_dir=options.model_dir,
                        confidence=options.hand_confidence,
                        focal_length=5000.0,
                    )
                    staged.mkdir(parents=True)
                    convert_bag(
                        options.bag, staged, spec, options.camera_config, backend,
                        max_image_skew_ms=options.max_image_skew_ms,
                        max_pose_bracket_ms=options.max_pose_bracket_ms,
                        projection_gate_px=options.projection_gate_px,
                        temporal_same_step_m=options.temporal_same_step_m,
                        temporal_advantage_m=options.temporal_advantage_m,
                    )
                    build_mode = "fresh_rosbag_conversion"
                apply_annotations(staged, spec)
                result = audit_dataset(staged, spec, decode_videos=options.decode_audit)
                if not base_dataset.is_dir():
                    base_entry.mkdir(parents=True, exist_ok=True)
                    _copy_reference(staged, base_dataset)
                    write_manifest(base_entry / "cache.json", {
                        "complete": True,
                        "base_key": base_key,
                        "created_unix_s": time.time(),
                        "inputs": base_payload,
                    })
                staged.replace(cached_dataset)
            if options.write_review_overlay:
                if options.reuse_overlay and options.reuse_overlay.is_file():
                    shutil.copy2(options.reuse_overlay, cached_overlay)
                else:
                    write_overlay(cached_dataset, cached_overlay)
            write_manifest(entry / "cache.json", {
                "complete": True,
                "cache_key": cache_key,
                "build_mode": build_mode,
                "created_unix_s": time.time(),
                "inputs": payload,
                "audit": result,
            })

    materialize_tree(cached_dataset, options.output)
    overlay_output = options.output.parent / f"{options.output.name}_overlay.mp4"
    if options.write_review_overlay and cached_overlay.is_file():
        try:
            overlay_output.hardlink_to(cached_overlay)
        except OSError:
            shutil.copy2(cached_overlay, overlay_output)
    result = audit_dataset(options.output, spec, decode_videos=options.decode_audit)
    result.update({
        "cache_hit": cache_hit,
        "cache_key": cache_key,
        "base_cache_key": base_key,
        "cache_entry": str(entry),
        "dataset": str(options.output),
        "overlay": str(overlay_output) if overlay_output.is_file() else None,
    })
    return result
