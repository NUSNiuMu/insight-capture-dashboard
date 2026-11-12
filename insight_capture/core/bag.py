"""Standard rosbag2 recordings with one MCAP data file per take."""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator

import yaml

MANIFEST_NAME = "recording_manifest.json"


@lru_cache(maxsize=256)
def _metadata(contents: bytes) -> dict:
    # Content keys also detect rewrites within one filesystem timestamp tick.
    payload = yaml.load(contents, Loader=yaml.CSafeLoader) or {}
    if not isinstance(payload, dict):
        return {}
    info = payload.get("rosbag2_bagfile_information", {})
    return info if isinstance(info, dict) else {}


def read_metadata(bag: Path) -> dict:
    try:
        return deepcopy(_metadata((Path(bag) / "metadata.yaml").read_bytes()))
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def single_mcap(bag: Path) -> Path:
    bag = Path(bag)
    info = read_metadata(bag)
    files = list(bag.glob("*.mcap"))
    relative = info.get("relative_file_paths", [])
    if (info.get("storage_identifier") != "mcap" or len(files) != 1
            or list(bag.glob("*.db3")) or len(relative) > 1):
        raise ValueError(f"Expected one standard MCAP recording: {bag}")
    if relative and (bag / relative[0]).resolve() != files[0].resolve():
        raise ValueError(f"MCAP metadata does not match its data file: {bag}")
    return files[0]


def is_mcap_bag(bag: Path) -> bool:
    try:
        single_mcap(bag)
    except ValueError:
        return False
    return True


def recording_timing(bag: Path) -> dict | None:
    """Read the saved data span, not preparation or finalization wall time."""
    try:
        single_mcap(bag)
        info = read_metadata(bag)
        start_ns = int(info["starting_time"]["nanoseconds_since_epoch"])
        duration_ns = int(info["duration"]["nanoseconds"])
        if int(info["message_count"]) < 1 or start_ns <= 0 or duration_ns < 0:
            return None
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None
    return {
        "source": "mcap_metadata",
        "started_at_epoch_s": start_ns / 1e9,
        "stopped_at_epoch_s": (start_ns + duration_ns) / 1e9,
        "duration_s": duration_ns / 1e9,
    }


def topic_types(bag: Path) -> dict[str, str]:
    single_mcap(bag)
    return {row["topic_metadata"]["name"]: row["topic_metadata"]["type"]
            for row in read_metadata(bag).get("topics_with_message_count", [])}


def recorded_topics(bag: Path) -> list[str]:
    return list(topic_types(bag))


def iter_messages(bag: Path, topics: Iterable[str]) -> Iterator[tuple[str, bytes, int]]:
    single_mcap(bag)
    requested = set(topics)
    if not requested:
        return
    import rosbag2_py
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
                rosbag2_py.ConverterOptions("", ""))
    reader.set_filter(rosbag2_py.StorageFilter(topics=sorted(requested)))
    while reader.has_next():
        topic, raw, stamp = reader.read_next()
        yield str(topic), bytes(raw), int(stamp)


def source_files(bag: Path) -> list[Path]:
    files = [Path(bag) / "metadata.yaml", single_mcap(bag)]
    manifest = Path(bag) / MANIFEST_NAME
    if manifest.is_file():
        files.append(manifest)
    return files
