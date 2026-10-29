"""Recover interrupted recordings into validated copies while retaining originals."""

from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Sequence

from rosbags.highlevel import AnyReader
from rosbags.typesys import Stores, get_typestore
import yaml

from insight_capture.legacy.composite_bag import MANIFEST_NAME, read_metadata

RECORDING_TARGET_NAME = ".recording_target.json"


class RecordingRecovery:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _recover_orphaned_stagings(self) -> None:
        staging_root = self.owner.rosbag_root / "_staging"
        if not staging_root.is_dir():
            return
        for staging in sorted(p for p in staging_root.iterdir() if p.is_dir()):
            if staging.name.startswith(".") or staging.name.endswith(".leftover"):
                continue
            with self.owner._lock:
                if self.owner._staging_dir == staging:
                    continue
            try:
                self._recover_one_staging(staging)
            except Exception as exc:
                self._recovery_log(f"{staging.name}: recovery failed; originals retained: {exc}")

    def _recover_one_staging(self, staging: Path) -> None:
        output = self._output_path_for_staging(staging)
        archive = staging.with_name("." + staging.name + ".original")
        if output.exists() or archive.exists():
            raise FileExistsError(f"recovery destination already exists: {output} or {archive}")
        with tempfile.TemporaryDirectory(prefix=".recovery-", dir=staging.parent) as directory:
            workspace = Path(directory)
            source = workspace / "source"
            shutil.copytree(staging, source)
            bags = [source] if (list(source.glob("*.mcap")) or list(source.glob("*.db3"))) else sorted(
                p for p in source.iterdir() if p.is_dir() and
                (list(p.glob("*.db3")) or list(p.glob("*.mcap")))
            )
            if not bags:
                raise RuntimeError("no recording data found")
            expected = Counter()
            types = {}
            for bag in bags:
                if list(bag.glob("*.mcap")) and list(bag.glob("*.db3")):
                    raise RuntimeError(f"mixed storage formats in one recording: {bag}")
                storage = "mcap" if list(bag.glob("*.mcap")) else "sqlite3"
                if storage == "sqlite3":
                    # Flush committed WAL records only in the working copy.
                    for database in bag.glob("*.db3"):
                        with closing(sqlite3.connect(database)) as connection:
                            busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                            if busy:
                                raise RuntimeError(f"copied SQLite WAL is busy: {database}")
                self._run(["ros2", "bag", "reindex", "-s", storage, str(bag)])
                counts, definitions = self._validate(bag)
                expected.update(counts)
                for topic, msgtype in definitions.items():
                    if topic in types and types[topic] != msgtype:
                        raise RuntimeError(f"conflicting message types for {topic}")
                    types[topic] = msgtype
            generated = workspace / "output"
            config = workspace / "convert.yaml"
            config.write_text(yaml.safe_dump({"output_bags": [
                {"uri": str(generated), "storage_id": "mcap", "all": True}
            ]}))
            command = ["ros2", "bag", "convert"]
            for bag in bags:
                command.extend(["-i", str(bag)])
            self._run([*command, "-o", str(config)])
            actual, actual_types = self._validate(generated)
            if actual != expected or actual_types != types:
                raise RuntimeError("recovered topic counts or types do not match the complete source")
            manifest = json.loads((source / MANIFEST_NAME).read_text()) if (source / MANIFEST_NAME).is_file() else {}
            manifest.update({
                "version": 3, "format": "rosbag2", "storage_id": "mcap",
                "bag_name": output.name, "recovered": True,
                "recovery_source": str(archive), "recovery_method": "reindex_and_convert",
                "message_count": sum(actual.values()), "topic_count": len(actual),
            })
            (generated / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
            os.rename(generated, output)
            os.rename(staging, archive)
        self._recovery_log(f"{staging.name}: complete recovery -> {output.name}; originals retained at {archive}")
        self.owner._notify_recording_completed(output)

    @staticmethod
    def _validate(bag: Path):
        metadata = read_metadata(bag)
        if not metadata:
            raise RuntimeError(f"missing recording metadata: {bag}")
        expected = Counter({row["topic_metadata"]["name"]: int(row["message_count"])
                            for row in metadata["topics_with_message_count"]})
        counts = Counter()
        with AnyReader([bag], default_typestore=get_typestore(Stores.ROS2_HUMBLE)) as reader:
            definitions = {c.topic: c.msgtype for c in reader.connections}
            for connection, _, _ in reader.messages():
                counts[connection.topic] += 1
        if not sum(counts.values()) or counts != expected:
            raise RuntimeError(f"recording contents do not match metadata: {bag}")
        return counts, definitions

    def _run(self, command: Sequence[str]) -> None:
        env = {**os.environ, "ROS_DOMAIN_ID": str(self.owner.ros_domain_id)}
        result = subprocess.run(command, capture_output=True, text=True, env=env)
        if result.returncode:
            raise RuntimeError(f"{' '.join(command[:3])} failed: {(result.stdout + result.stderr)[-2000:]}")

    def _output_path_for_staging(self, staging: Path) -> Path:
        marker = staging / RECORDING_TARGET_NAME
        metadata = json.loads(marker.read_text()) if marker.is_file() else {}
        subdirectory = str(metadata.get("output_subdirectory") or "").strip()
        if subdirectory and not re.fullmatch(r"[0-9A-Za-z_-]+", subdirectory):
            raise ValueError("invalid recovery output subdirectory")
        root = self.owner.rosbag_root / subdirectory
        root.mkdir(parents=True, exist_ok=True)
        return root / (staging.name + "_recovered")

    def _recovery_log(self, message: str) -> None:
        line = f"[recovery] {message}"
        self.owner._output_lines.append(line)
        print(line, flush=True)
