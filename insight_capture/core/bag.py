"""Standard rosbag2 recordings with one MCAP data file per take."""

from pathlib import Path
from typing import Iterable, Iterator

import yaml

MANIFEST_NAME = "recording_manifest.json"


def read_metadata(bag: Path) -> dict:
    try:
        payload = yaml.safe_load((Path(bag) / "metadata.yaml").read_text()) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    info = payload.get("rosbag2_bagfile_information", {})
    return info if isinstance(info, dict) else {}


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
