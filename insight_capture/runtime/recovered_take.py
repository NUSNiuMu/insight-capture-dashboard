"""Read-only association of failed Takes with validated recovery outputs."""
import json
from pathlib import Path

from insight_capture.core.bag import MANIFEST_NAME, read_metadata, recording_timing, single_mcap


def recovered_take_info(take: dict) -> dict | None:
    if take.get('state') != 'finalization_failed' or not take.get('bag_path'):
        return None
    try:
        original = Path(take['bag_path'])
        bag = original.with_name(original.name + '_recovered')
        manifest = json.loads((bag / MANIFEST_NAME).read_text())
        if not isinstance(manifest, dict) or manifest.get('recovered') is not True:
            return None
        if (manifest.get('bag_name') != bag.name or
                Path(manifest.get('recovery_source', '')).name != '.' + original.name + '.original'):
            return None
        timing = recording_timing(bag)
        metadata = read_metadata(bag)
        count = int(metadata.get('message_count', 0))
        if timing is None or count <= 0 or count != manifest.get('message_count'):
            return None
        paths = {'data': single_mcap(bag), 'metadata': bag / 'metadata.yaml',
                 'manifest': bag / MANIFEST_NAME}
        if not all(path.is_file() for path in paths.values()):
            return None
        return {
            'bag_path': str(bag), 'bag_name': bag.name,
            'original_path': manifest['recovery_source'],
            'scope': 'readable_messages_only', 'message_count': count,
            'recording_timing': timing,
            'files': [{'kind': kind, 'name': path.name, 'size_bytes': path.stat().st_size}
                      for kind, path in paths.items()],
        }
    except (OSError, ValueError, TypeError):
        return None
