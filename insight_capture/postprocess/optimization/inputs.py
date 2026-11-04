"""Discover reconstruction inputs from the recorded MCAP metadata."""

from pathlib import Path

from insight_capture.core.bag import read_metadata, single_mcap


def recording_inputs(bag: Path) -> list[dict]:
    single_mcap(bag)
    topics = {row['topic_metadata']['name']: row['topic_metadata']['type']
              for row in read_metadata(bag).get('topics_with_message_count', [])
              if row.get('message_count', 0) > 0}
    cameras = {}
    labels = {'infra1': 'Left IR', 'infra2': 'Right IR', 'color': 'Color',
              'stereo/left': 'Front left', 'stereo/right': 'Front right',
              'left': 'Side left', 'right': 'Side right'}
    for topic, kind in topics.items():
        if kind not in {'sensor_msgs/msg/Image', 'sensor_msgs/msg/CompressedImage'}:
            continue
        if '/camera/' not in topic or '/image_' not in topic or '/depth/' in topic:
            continue
        prefix, stream = topic.split('/camera/', 1)
        name = prefix.strip('/')
        vio = prefix + '/camera/vio_100hz'
        info = topic.rsplit('/image_', 1)[0] + '/camera_info'
        if (topics.get(vio) != 'geometry_msgs/msg/PoseStamped'
                or topics.get(info) != 'sensor_msgs/msg/CameraInfo'):
            continue
        camera = cameras.setdefault(name, {'name': name, 'label': name,
                                          'vio_topic': vio, 'streams': []})
        view = stream.split('/image_', 1)[0]
        codec = 'H.264' if topic.endswith('/h264') else 'JPEG' if kind.endswith('CompressedImage') else 'Raw'
        camera['streams'].append({'topic': topic, 'camera_info_topic': info,
                                  'label': labels.get(view, view) + ' · ' + codec})
    return list(cameras.values())
