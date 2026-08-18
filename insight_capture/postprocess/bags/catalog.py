"""Read-only rosbag catalog and result badge helpers."""

import json
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from insight_capture.legacy.composite_bag import MANIFEST_NAME, aggregate_metadata

try:
    import yaml
except Exception:  # pragma: no cover - metadata parsing degrades gracefully
    yaml = None


@dataclass(frozen=True)
class BagLocation:
    """A rosbag discovered below an allowed storage root."""

    bag_id: str
    path: Path
    root: Path
    relative_path: str
    current: bool


class BagLibrary:
    """Resolve stable bag references across recording and fallback directories."""

    def __init__(
        self,
        roots_provider: Callable[[], Iterable[Path]],
        current_root_provider: Callable[[], Path],
    ) -> None:
        self._roots_provider = roots_provider
        self._current_root_provider = current_root_provider

    @staticmethod
    def _id_for_path(path: Path) -> str:
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        return f"bag-{digest[:24]}"

    def _roots(self) -> list[Path]:
        roots: list[Path] = []
        for candidate in self._roots_provider():
            root = Path(candidate).resolve()
            if root not in roots:
                roots.append(root)
        return roots

    @staticmethod
    def _discover(root: Path) -> Iterable[Path]:
        if not root.is_dir():
            return
        for directory, child_names, _file_names in os.walk(root, followlinks=False):
            path = Path(directory)
            child_names[:] = [
                name
                for name in child_names
                if not name.startswith(".") and name not in {"_outputs", "_staging", "review"}
            ]
            if (path / "metadata.yaml").is_file() or (path / MANIFEST_NAME).is_file():
                child_names[:] = []
                yield path.resolve()

    def locations(self, scope: str = "all") -> list[BagLocation]:
        current_root = self._current_root_provider().resolve()
        library_roots = self._roots()
        roots = [current_root] if scope == "current" else library_roots
        seen: set[Path] = set()
        locations: list[BagLocation] = []
        for root in roots:
            for path in self._discover(root):
                if path in seen:
                    continue
                seen.add(path)
                containing_root = next(
                    (candidate for candidate in library_roots if path == candidate or candidate in path.parents),
                    root,
                )
                locations.append(
                    BagLocation(
                        bag_id=self._id_for_path(path),
                        path=path,
                        root=containing_root,
                        relative_path=path.relative_to(containing_root).as_posix(),
                        current=path == current_root or current_root in path.parents,
                    )
                )
        return sorted(
            locations,
            key=lambda item: item.path.stat().st_mtime if item.path.exists() else 0,
            reverse=True,
        )

    def resolve(self, reference: str) -> Path:
        reference = str(reference or "").strip()
        if not reference:
            raise ValueError("Bag reference is required.")
        locations = self.locations("all")
        by_id = next((item for item in locations if item.bag_id == reference), None)
        if by_id is not None:
            return by_id.path
        # Keep existing clients compatible while bag names remain unique.
        by_name = [item for item in locations if item.path.name == reference]
        if len(by_name) == 1:
            return by_name[0].path
        if len(by_name) > 1:
            raise ValueError(f"Bag name is ambiguous across recording directories: {reference}")
        raise FileNotFoundError(f"Bag not found: {reference}")

    def reference_for_path(self, path: Path) -> Optional[str]:
        resolved = Path(path).resolve()
        if not (
            (resolved / "metadata.yaml").is_file()
            or (resolved / MANIFEST_NAME).is_file()
        ):
            return None
        for root in self._roots():
            if resolved == root or root in resolved.parents:
                return self._id_for_path(resolved) if resolved.is_dir() else None
        return None

def _format_bytes(size_bytes: int) -> str:
    value = float(max(int(size_bytes), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0


def _directory_size_bytes(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat().st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _result_exists(results_root: Path, category: str, bag_name: str) -> bool:
    candidates = [
        results_root / category / f"{bag_name}.json",
        results_root / category / bag_name,
        results_root / f"{bag_name}_{category}.json",
    ]
    return any(candidate.exists() for candidate in candidates)


def _read_bag_metadata(metadata_path: Path) -> Dict[str, object]:
    if yaml is None or not metadata_path.exists():
        return {}
    try:
        # Prefer libyaml because every bag-list request parses all metadata.
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        payload = yaml.load(metadata_path.read_text(encoding="utf-8"), Loader=loader) or {}
    except Exception:
        return {}
    info = payload.get("rosbag2_bagfile_information", {})
    return info if isinstance(info, dict) else {}


def list_rosbag_locations(
    locations: Iterable[BagLocation], results_root: Path
) -> List[Dict[str, object]]:
    entries: List[Dict[str, object]] = []
    for location in locations:
        bag_dir = location.path
        metadata = aggregate_metadata(bag_dir) or _read_bag_metadata(bag_dir / "metadata.yaml")
        if not metadata:
            continue
        duration_ns = int((metadata.get("duration") or {}).get("nanoseconds", 0) or 0)
        message_count = int(metadata.get("message_count", 0) or 0)
        topics = metadata.get("topics_with_message_count") or []
        size_bytes = _directory_size_bytes(bag_dir)
        scored = _result_exists(results_root, "scores", bag_dir.name) or _result_exists(results_root, "scoring", bag_dir.name)
        # The persisted report distinguishes pass, fail, and never checked.
        integrity: Optional[bool] = None
        integrity_path = results_root / "integrity" / f"{bag_dir.name}.json"
        if integrity_path.exists():
            try:
                integrity = bool(json.loads(integrity_path.read_text()).get("ok"))
            except (OSError, ValueError, AttributeError):
                integrity = None
        review_state = "missing"
        review_quality = None
        review_manifest_path = bag_dir / "review" / "manifest.json"
        if review_manifest_path.is_file() and (bag_dir / "review" / "review.mp4").is_file():
            try:
                review_manifest = json.loads(review_manifest_path.read_text())
                if review_manifest.get("schema_version") == 5:
                    review_state = "ready"
                    review_quality = (review_manifest.get("quality") or {}).get("state")
                else:
                    review_state = "invalid"
            except (OSError, ValueError, AttributeError):
                review_state = "invalid"
        elif (bag_dir / ".review.preparing").is_dir():
            review_state = "building"
        entries.append(
            {
                "id": location.bag_id,
                "name": bag_dir.name,
                "relative_path": location.relative_path,
                "root": str(location.root),
                "current": location.current,
                "size_label": _format_bytes(size_bytes),
                "duration_s": duration_ns / 1_000_000_000.0,
                "message_count": message_count,
                "topic_count": len(topics) if isinstance(topics, list) else 0,
                "scored": scored,
                "integrity": integrity,
                "review_state": review_state,
                "review_quality": review_quality,
                "label": "scored" if scored else "unscored",
            }
        )
    return entries


def list_rosbags(rosbag_root: Path, results_root: Path) -> List[Dict[str, object]]:
    """List direct child bags for legacy callers."""

    root = Path(rosbag_root).resolve()
    if not root.exists():
        return []
    locations = []
    for bag_dir in sorted(
        root.iterdir(),
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    ):
        if not bag_dir.is_dir():
            continue
        if not (bag_dir / "metadata.yaml").exists() and not (bag_dir / MANIFEST_NAME).exists():
            continue
        locations.append(
            BagLocation(
                bag_id=BagLibrary._id_for_path(bag_dir),
                path=bag_dir.resolve(),
                root=root,
                relative_path=bag_dir.name,
                current=True,
            )
        )
    return list_rosbag_locations(locations, results_root)
