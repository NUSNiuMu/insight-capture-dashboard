#!/usr/bin/env python3
"""Retry a saved distributed take using its durable session manifest."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from insight_capture.runtime.recording.distributed_merge import merge_session, write_json
from insight_capture.runtime.recording.ego_participant import EgoParticipant


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/cameras.json"))
    args = parser.parse_args()
    session = json.loads(args.session.read_text())
    config = json.loads(args.config.read_text())["distributed_recording"]
    if not session.get("local_saved") or not session.get("remote_saved"):
        parser.error("Both participants must have confirmed durable recordings before merging")
    if session["ego_url"] != config["ego_url"].rstrip("/"):
        parser.error("Session belongs to a different Ego origin")
    output = Path(session["output_path"])
    if output.exists():
        parser.error("Output already exists; it will not be overwritten")
    participant = EgoParticipant(config)
    copied = Path(session["ego_copy_path"])
    session["ego_copy_verification"] = participant.download(session, copied)
    output.parent.mkdir(parents=True, exist_ok=True)
    session["alignment"] = merge_session(session, config, copied, Path(session["jetson_path"]), output)
    session.update(phase="done", error=None)
    write_json(args.session, session)
    write_json(output / "distributed_session.json", session)
    print(json.dumps({"output_path": str(output), "time_alignment": session["alignment"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
