"""Remove obsolete mounts that replace code shipped in the Release images."""

import json
from pathlib import Path, PurePosixPath
from posixpath import normpath
import sys

WORKSPACE = PurePosixPath("/workspaces/insight_capture")
CODE = {".native-build", "deploy", "insight_capture", "native_dashboard", "scripts", "web_dashboard"}
MAPPER = "/usr/local/bin/insight_mapping"
OVERRIDES = ("compose.override.yml", "compose.override.yaml",
             "docker-compose.override.yml", "docker-compose.override.yaml")


def shadows_code(service, target):
    if not isinstance(target, str):
        return False
    path = PurePosixPath(normpath(target))
    return (path == WORKSPACE
            or (path.is_relative_to(WORKSPACE) and path.relative_to(WORKSPACE).parts[0] in CODE)
            or (service == "insight9-sparse-mapper" and target == MAPPER))


def mount_target(volume):
    if isinstance(volume, dict):
        return volume.get("target")
    if isinstance(volume, str):
        # Named/host source and anonymous mounts are all capable of shadowing files.
        return volume.split(":")[1] if ":" in volume else volume
    return None


def stage_overrides(directory):
    import yaml

    for name in OVERRIDES:
        path = directory / name
        if not path.is_file():
            continue
        document = yaml.compose(path.read_text())
        if not isinstance(document, yaml.MappingNode):
            continue
        services = value(yaml, document, "services")
        if not isinstance(services, yaml.MappingNode):
            continue
        changed = False
        for service, settings in services.value:
            volumes = value(yaml, settings, "volumes")
            if not isinstance(volumes, yaml.SequenceNode):
                continue
            kept = []
            for mount in volumes.value:
                if isinstance(mount, yaml.ScalarNode):
                    target = mount_target(mount.value)
                else:
                    node = value(yaml, mount, "target")
                    target = node.value if isinstance(node, yaml.ScalarNode) else None
                if shadows_code(service.value, target):
                    print(f"Retiring old code mount {name}: {service.value} {target}")
                else:
                    kept.append(mount)
            if len(kept) != len(volumes.value):
                replacement = yaml.SequenceNode(volumes.tag, kept, flow_style=volumes.flow_style)
                settings.value = [(key, replacement if key.value == "volumes" else node)
                                  for key, node in settings.value]
                changed = True
        if changed:
            path.write_text(yaml.serialize(document))


def value(yaml, mapping, name):
    if isinstance(mapping, yaml.MappingNode):
        return next((v for k, v in mapping.value if k.value == name), None)
    return None


def check_config(settings, version):
    services = settings["services"]
    dashboard = services["insight-dashboard"]
    if dashboard.get("image") != f"insight-dashboard:{version}":
        raise ValueError("The effective Dashboard image is not the image in this Release")
    for service, config in services.items():
        for volume in config.get("volumes", []):
            target = mount_target(volume)
            if shadows_code(service, target):
                raise ValueError(f"{service} still mounts over packaged code at {target}")
    port = int(dashboard.get("environment", {}).get("DASHBOARD_PORT", 8765))
    if not 0 < port < 65536:
        raise ValueError("Invalid Dashboard port")
    return port


if __name__ == "__main__":
    if sys.argv[1] == "stage":
        stage_overrides(Path(sys.argv[2]))
    else:
        try:
            print(check_config(json.load(sys.stdin), sys.argv[2]))
        except (KeyError, ValueError, TypeError) as exc:
            sys.exit(f"Invalid Release Compose configuration: {exc}")
