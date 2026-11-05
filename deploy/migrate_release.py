#!/usr/bin/env python3
"""Convert installed Release configuration once; runtime uses the current schema."""

import argparse
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import shutil


def migrate(root: Path, templates: Path) -> dict:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    backup = config / (".upgrade-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    changed, warnings = [], []

    def read(path, default):
        try:
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                return value
            raise ValueError("expected a JSON object")
        except FileNotFoundError:
            return deepcopy(default)
        except (ValueError, OSError) as exc:
            warnings.append(f"{path}: {exc}; original retained")
            return deepcopy(default)

    def write(path, value):
        try:
            if path.is_file() and json.loads(path.read_text()) == value:
                return
        except (ValueError, OSError):
            pass
        previous = path.stat() if path.exists() else None
        if previous:
            saved = backup / path.relative_to(root)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + ".upgrade")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        if previous:
            os.chmod(temporary, previous.st_mode & 0o777)
        if os.geteuid() == 0:
            owner = previous or root.stat()
            os.chown(temporary, owner.st_uid, owner.st_gid)
        temporary.replace(path)
        changed.append(str(path.relative_to(root)))

    def fill(default, current):
        result = deepcopy(default)
        if not isinstance(current, dict):
            return result
        for key, value in current.items():
            result[key] = fill(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
        return result

    cameras = read(config / "cameras.json", {})
    head = next((c for c in cameras.get("cameras", []) if c.get("teleop_role") == "head"), {})
    profile = "jetson-nx-egohalo" if "ego" in str(head.get("name", "")).lower() or cameras.get("egohalo_rectification") else "jetson-nx"
    defaults = read(templates / "devices" / profile / "cameras.json", {})
    legacy = not cameras.get("mapping")
    if legacy:
        upgraded = deepcopy(defaults)
        for key in ("ros_domain_id", "camera_dds_type", "dashboard"):
            if key in cameras:
                upgraded[key] = fill(upgraded[key], cameras[key]) if isinstance(upgraded[key], dict) else cameras[key]
        old = {c["name"]: c for c in cameras.get("cameras", [])}
        routing = {"dashboard_pose_stream", "record_topics", "dashboard_streams"}
        for camera in upgraded["cameras"]:
            site = old.get(camera["name"], {})
            for key in (set(camera) | {"avatar_scale", "avatar_offset_xyz", "avatar_rotation_deg_xyz"}) - routing:
                if key in site:
                    camera[key] = site[key]
            model = camera.get("avatar_model", "")
            camera["avatar_model"] = model.replace("iron-man_helmet_mk3_clean.glb", "iron-man_helmet_mk3_optimized.glb")
        cameras = upgraded
        write(config / "cameras.json", cameras)
    runtime_defaults = read(templates / "devices" / profile / "runtime.json", {})
    runtime_path = config / "runtime.json"
    old_runtime = read(runtime_path if runtime_path.exists() else config / "post_processing.json", {})
    runtime = {key: fill(value, old_runtime.get(key, {})) if isinstance(value, dict) else old_runtime.get(key, value)
               for key, value in runtime_defaults.items()}
    runtime["capture_session"].pop("task", None)
    write(config / "runtime.json", runtime)
    tasks = read(config / "capture_tasks.json", read(templates / "capture_tasks.json", {}))
    results = Path(runtime["results_dir"])
    project = Path("/workspaces/insight_capture")
    if results.is_relative_to(project):
        results = root / results.relative_to(project)
    sessions = (results if results.is_absolute() else root / results) / "sessions"
    active = read(sessions / "active_session.json", {})
    task_id = str(active.get("task_id") or "")
    if task_id:
        if not any(t.get("id", t.get("task_id")) == task_id for t in tasks.get("tasks", [])):
            tasks.setdefault("tasks", []).append(dict(id=task_id, name=task_id,
                instruction=f"Capture {task_id}", capture_profile="capture"))
        previous = str(active.get("session_id") or task_id)
        if previous != task_id and Path(previous).name == previous:
            source, destination = sessions / previous, sessions / task_id
            known = {read(p, {}).get("bag_path") for p in (destination / "takes").glob("take_*.json")}
            next_id = max((int(p.stem.split("_")[-1]) for p in (destination / "takes").glob("take_*.json")), default=0) + 1
            for path in sorted((source / "takes").glob("take_*.json")):
                take = read(path, {})
                if not take or take.get("bag_path") and take["bag_path"] in known:
                    continue
                take.update(session_id=task_id, take_id=next_id)
                write(destination / "takes" / f"take_{next_id:04d}.json", take)
                known.add(take.get("bag_path")); next_id += 1
            metadata = read(source / "session.json", {})
            if metadata and not (destination / "session.json").exists():
                metadata["session_id"] = task_id
                write(destination / "session.json", metadata)
            active["session_id"] = task_id
            write(sessions / "active_session.json", active)
    for task in tasks.get("tasks", []):
        task.pop("speech_name", None)
        task.pop("voice_aliases", None)
    for path in sessions.glob("*/task.json"):
        task = read(path, {})
        if "speech_name" in task or "voice_aliases" in task:
            task.pop("speech_name", None)
            task.pop("voice_aliases", None)
            write(path, task)
    write(config / "capture_tasks.json", tasks)
    marker = config / ".device"
    if not marker.exists() or marker.read_text().strip() != profile:
        marker.write_text(profile + "\n")
    return {"profile": profile, "changed": changed, "warnings": warnings,
            "backup": str(backup) if backup.exists() else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--templates", type=Path, default=Path(__file__).resolve().parents[1] / "config")
    args = parser.parse_args()
    print(json.dumps(migrate(args.root.resolve(), args.templates.resolve()), ensure_ascii=False, indent=2))
