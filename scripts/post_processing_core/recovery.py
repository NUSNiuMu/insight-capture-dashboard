"""Power-loss staging recovery, reindex, salvage, and merge."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Sequence

from .sqlite_merge import merge_sqlite_parts
from .composite_bag import COMPOSITE_FORMAT, MANIFEST_NAME, read_metadata

try:
    import yaml
except Exception:  # pragma: no cover - recovery degrades gracefully
    yaml = None


class RecordingRecovery:
    def __init__(self, owner) -> None:
        self.owner = owner

    def _recover_orphaned_stagings(self) -> None:
        """Recover orphaned staging parts and retain anything unsalvageable."""
        staging_root = self.owner.rosbag_root / "_staging"
        if not staging_root.is_dir():
            return
        for staging_dir in sorted(p for p in staging_root.iterdir() if p.is_dir()):
            if staging_dir.name.endswith(".leftover"):
                continue  # already partially recovered on an earlier boot
            with self.owner._lock:
                if self.owner._staging_dir == staging_dir:
                    continue  # active recording, not an orphan
            try:
                self.owner._recover_one_staging(staging_dir)
            except Exception as exc:  # noqa: BLE001 - one bad orphan must not stop the rest
                self.owner._recovery_log(f"{staging_dir.name}: recovery failed, leaving data in place: {exc}")

    def _recover_one_staging(self, staging_dir: Path) -> None:
        part_dirs = sorted(
            p for p in staging_dir.iterdir()
            if p.is_dir() and (list(p.glob("*.db3")) or list(p.glob("*.mcap")))
        )
        if not part_dirs:
            self.owner._recovery_log(f"{staging_dir.name}: no bag data at all, removing empty leftover")
            shutil.rmtree(staging_dir, ignore_errors=True)
            return
        self.owner._recovery_log(f"{staging_dir.name}: adopting interrupted recording ({len(part_dirs)} part bags)")
        good_parts: List[Path] = []
        for part in part_dirs:
            if (part / "metadata.yaml").is_file() or self.owner._reindex_part(part):
                good_parts.append(part)
                continue
            if list(part.glob("*.db3")) and self.owner._salvage_part(part) and self.owner._reindex_part(part):
                good_parts.append(part)
            else:
                self.owner._recovery_log(f"{staging_dir.name}/{part.name}: unrecoverable, leaving for forensics")
        if not good_parts:
            self.owner._recovery_log(f"{staging_dir.name}: nothing recoverable; staging dir kept")
            return
        output_path = self.owner.rosbag_root / staging_dir.name
        if output_path.exists():
            output_path = self.owner.rosbag_root / f"{staging_dir.name}_recovered_{time.strftime('%Y%m%d_%H%M%S')}"
        if any(list(part.glob("*.mcap")) for part in good_parts):
            self._publish_composite_recovery(staging_dir, good_parts, part_dirs, output_path)
            self.owner._recovery_log(
                f"{staging_dir.name}: recovered {len(good_parts)}/{len(part_dirs)} MCAP part bags -> {output_path.name}"
            )
            self.owner._notify_recording_completed(output_path)
            return
        try:
            self._convert_merge(good_parts, output_path)
        except Exception:
            # Probe parts individually because one corrupt part can crash conversion.
            self.owner._recovery_log(f"{staging_dir.name}: merged convert failed; probing parts individually")
            probed = [p for p in good_parts if self.owner._part_converts_cleanly(p)]
            dropped = [p.name for p in good_parts if p not in probed]
            if dropped:
                self.owner._recovery_log(f"{staging_dir.name}: excluding poisoned part(s): {', '.join(dropped)}")
            if not probed:
                self.owner._recovery_log(f"{staging_dir.name}: no part survives conversion; staging dir kept")
                return
            good_parts = probed
            self._convert_merge(good_parts, output_path)
        if len(good_parts) == len(part_dirs):
            shutil.rmtree(staging_dir, ignore_errors=True)
        else:
            # Mark partial leftovers so later boots do not recover them again.
            for part in good_parts:
                shutil.rmtree(part, ignore_errors=True)
            staging_dir.rename(staging_dir.with_name(f"{staging_dir.name}.leftover"))
        self.owner._recovery_log(
            f"{staging_dir.name}: recovered {len(good_parts)}/{len(part_dirs)} part bags -> {output_path.name}"
        )
        self.owner._notify_recording_completed(output_path)

    def _part_converts_cleanly(self, part: Path) -> bool:
        probe_dir = part.parent / f".{part.name}.convert_probe"
        shutil.rmtree(probe_dir, ignore_errors=True)
        try:
            self._convert_merge([part], probe_dir)
            return True
        except Exception:
            return False
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    def _reindex_part(self, part: Path) -> bool:
        env = os.environ.copy()
        env["ROS_DOMAIN_ID"] = str(self.owner.ros_domain_id)
        storage_id = "mcap" if list(part.glob("*.mcap")) else "sqlite3"
        result = subprocess.run(
            ["ros2", "bag", "reindex", "-s", storage_id, str(part)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
        return result.returncode == 0 and (part / "metadata.yaml").is_file()

    def _publish_composite_recovery(
        self, staging_dir: Path, good_parts: Sequence[Path],
        all_parts: Sequence[Path], output_path: Path,
    ) -> None:
        parts = []
        for part in good_parts:
            info = read_metadata(part)
            parts.append({
                "name": part.name,
                "path": part.name,
                "storage_id": str(info.get("storage_identifier") or "mcap"),
                "message_count": int(info.get("message_count", 0) or 0),
                "topic_count": len(info.get("topics_with_message_count") or []),
                "starting_time_ns": int(
                    (info.get("starting_time") or {}).get("nanoseconds_since_epoch", 0) or 0
                ),
                "duration_ns": int((info.get("duration") or {}).get("nanoseconds", 0) or 0),
            })
        manifest = {
            "version": 2,
            "format": COMPOSITE_FORMAT,
            "storage_id": "mcap",
            "bag_name": output_path.name,
            "recovered": True,
            "parts": parts,
        }
        if len(good_parts) == len(all_parts):
            (staging_dir / MANIFEST_NAME).write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(staging_dir, output_path)
            return
        output_path.mkdir(parents=True)
        for part in good_parts:
            os.replace(part, output_path / part.name)
        (output_path / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        staging_dir.rename(staging_dir.with_name(f"{staging_dir.name}.leftover"))

    def _salvage_part(self, part: Path) -> bool:
        """Stream readable SQLite pages into a validated replacement database."""
        db_files = sorted(part.glob("*.db3"))
        if not db_files:
            return False
        source = db_files[0]
        rebuilt = source.with_suffix(".db3.rebuilt")
        rebuilt.unlink(missing_ok=True)
        dump = subprocess.Popen(["sqlite3", str(source), ".recover"], stdout=subprocess.PIPE)
        load = subprocess.Popen(["sqlite3", str(rebuilt)], stdin=dump.stdout)
        dump.stdout.close()
        load.wait()
        dump.wait()
        try:
            import sqlite3 as _sqlite3

            conn = _sqlite3.connect(f"file:{rebuilt}?mode=ro", uri=True)
            messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
            conn.close()
        except Exception:
            messages = 0
        if messages <= 0:
            rebuilt.unlink(missing_ok=True)
            return False
        # Clear stale WAL/journal companions along with the corrupt db --
        # they belong to the old file and would poison the rebuilt one.
        for leftover in part.glob(f"{source.name}-*"):
            leftover.unlink(missing_ok=True)
        source.unlink()
        rebuilt.rename(source)
        self.owner._recovery_log(f"{part.parent.name}/{part.name}: salvaged {messages} messages from malformed database")
        return True

    def _recovery_log(self, message: str) -> None:
        line = f"[recovery] {message}"
        self.owner._output_lines.append(line)
        print(line, flush=True)

    def merge_recording_parts(
        self, part_bags: Sequence[Path], output_path: Path
    ) -> Dict[str, object]:
        """Use bulk SQLite copying, with the official converter as a safe fallback."""
        try:
            return merge_sqlite_parts(part_bags, output_path)
        except Exception as exc:  # noqa: BLE001 - fallback preserves recording completion
            self.owner._output_lines.append(
                f"[merge] Fast SQLite merge unavailable ({exc}); falling back to ros2 bag convert"
            )
            started = time.perf_counter()
            self._convert_merge(part_bags, output_path)
            return {
                "method": "ros2_convert",
                "trim_applied": False,
                "timings": {"total_sec": round(time.perf_counter() - started, 3)},
            }

    def _convert_merge(self, part_bags: Sequence[Path], output_path: Path) -> None:
        if output_path.exists():
            shutil.rmtree(output_path)
        config_path = output_path.parent / f".{output_path.name}.convert.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump({"output_bags": [{"uri": str(output_path), "storage_id": "sqlite3", "all": True}]})
            if yaml is not None
            else f'output_bags:\n  - uri: "{output_path}"\n    storage_id: sqlite3\n    all: true\n'
        )
        cmd = ["ros2", "bag", "convert"]
        for bag in part_bags:
            cmd += ["-i", str(bag)]
        cmd += ["-o", str(config_path)]
        env = os.environ.copy()
        env["ROS_DOMAIN_ID"] = str(self.owner.ros_domain_id)
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        config_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(f"ros2 bag convert failed (exit {result.returncode}): {result.stdout[-2000:]}")
