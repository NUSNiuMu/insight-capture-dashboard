#!/usr/bin/env python3
"""Remove superseded Release artifacts after the new services are healthy."""

import argparse
import json
from pathlib import Path
import re
import subprocess


VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)$")
PACKAGE = re.compile(r"insight-dashboard(?:-deploy)?-(v\d+\.\d+\.\d+)\.(?:run|tar\.gz)$")


def version(value):
    match = VERSION.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True)


def cleanup(root, release, services, installer=None, previous_inference=""):
    report = dict(release=release, removed_images=[], removed_packages=[], skipped=[], warnings=[])
    current = version(release)
    if current is None:
        raise ValueError(f"Unsupported release version: {release}")
    ids = docker("compose", "ps", "--all", "--quiet").split()
    containers = json.loads(docker("inspect", *ids)) if ids else []
    images = json.loads(docker("image", "inspect", f"insight-dashboard:{release}",
                               f"insight-loopervio:{release}", "insight-superglue-validation:25.04"))
    expected = {"insight9-sparse-mapper": images[1]["Id"], "superglue-inference": images[2]["Id"]}
    running = {c["Config"]["Labels"]["com.docker.compose.service"] for c in containers
               if c["State"]["Status"] == "running"
               and c["State"].get("Health", {}).get("Status", "healthy") == "healthy"
               and c["Image"] == expected.get(c["Config"]["Labels"]["com.docker.compose.service"], images[0]["Id"])}
    if not set(services) <= running:
        report["skipped"].append("New services are not all ready; old artifacts retained")
        return report
    ids = docker("ps", "--all", "--quiet").split()
    used = {c["Image"] for c in json.loads(docker("inspect", *ids))} if ids else set()
    candidates = {}
    for line in docker("image", "ls", "--all", "--no-trunc", "--format", "{{.Repository}} {{.Tag}} {{.ID}}").splitlines():
        repository, tag, image_id = line.split()
        old = version(tag)
        if repository in {"insight-dashboard", "insight-loopervio"} and old and old < current:
            candidates[f"{repository}:{tag}"] = image_id
        if repository == tag == "<none>" and image_id == previous_inference:
            candidates[image_id] = image_id
    for reference, image_id in candidates.items():
        if image_id in used:
            report["skipped"].append(f"Image still used by a container: {reference}")
            continue
        try:
            docker("image", "rm", reference)
            report["removed_images"].append(reference)
        except subprocess.CalledProcessError as exc:
            report["warnings"].append(f"Could not remove image {reference}: {exc}")
    directories = {root, root / "release"}
    if installer:
        directories.add(installer.parent)
    for directory in sorted(directories):
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.is_symlink() or path == installer:
                continue
            match = PACKAGE.fullmatch(path.name)
            if not (match and version(match[1]) < current or path.name == "insight-superglue-validation-25.04.tar.gz"):
                continue
            try:
                path.unlink()
                report["removed_packages"].append(str(path))
            except OSError as exc:
                report["warnings"].append(f"Could not remove package {path}: {exc}")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("release")
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--previous-inference", default="")
    parser.add_argument("--services", nargs="+", required=True)
    args = parser.parse_args()
    report = cleanup(args.root.resolve(), args.release, args.services,
                     args.installer.resolve() if args.installer else None, args.previous_inference)
    (args.root / ".upgrade-cleanup.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
