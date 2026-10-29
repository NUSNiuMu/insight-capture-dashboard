"""Ensure native VIO readers never observe a partially committed visual revision."""
from types import SimpleNamespace
import threading
import time
from unittest.mock import patch

import numpy as np
from insight_capture.runtime.mapping.geometry import PoseSample
from insight_capture.runtime.mapping.looper_node import LooperNode, CameraState
from insight_capture.runtime.mapping.rig import LatestViewQueue


def main():
    node = SimpleNamespace(head='head', names=('head', 'insight3_a', 'insight3_b'),
                           generation=1, stop=threading.Event(), lock=threading.RLock(),
                           last_geometry=0., configure=lambda: True)
    node.stats = {}
    node.get_logger = lambda: SimpleNamespace(error=print)
    node.cameras = {n: CameraState(correction=np.eye(4)) for n in node.names}
    node.work = LatestViewQueue(node.names)
    sample = PoseSample(1, np.zeros(3), np.array([0., 0., 0., 1.]))
    for state in node.cameras.values():
        state.buffer.lookup = lambda _: sample
    node.features = SimpleNamespace(match=lambda *_: None,
        extract=lambda _: SimpleNamespace(keypoints=None, descriptors=None))
    block_head = threading.Event()
    release_head = threading.Event()
    node.publish_geometry = lambda *_: None
    calls = {n: threading.Event() for n in node.names}
    def transform(x):
        T = np.eye(4); T[0, 3] = x
        return T.tolist()
    def request(item, command, **_):
        calls[item.view].set()
        if command == 'geometry':
            assert positions() == [1, 2, 3], 'geometry preceded pose commit'
            return {'arrays': []}
        if command == 'map':
            if block_head.is_set():
                assert release_head.wait(2)
            return dict(map_from_imu=transform(1), map_revision=12, landmarks=100,
                        keyframes=10, raw_landmarks=100, wrist_updates=[
                            dict(camera_id=c, localized=True, map_revision=12, map_from_imu=transform(c),
                                 odom_from_imu=np.eye(4).tolist()) for c in (2, 3)])
        return dict(localized=True, map_revision=12, map_from_imu=transform(10 if item.view == node.names[1] else 20))
    node.request = request
    image = SimpleNamespace(encoding='mono8', height=4, width=4, step=4, data=bytes(16))
    def put(name):
        calls[name].clear()
        node.work.put_nowait(SimpleNamespace(view=name, stamp_ns=1, generation=node.generation,
                                             images=(image, image) if name == node.head else (image,)))
        assert calls[name].wait(2), name
        time.sleep(.02)
    def positions():
        with node.lock:
            return [node.cameras[n].correction[0, 3] for n in node.names]
    with patch('insight_capture.runtime.mapping.looper_node.stereo_arrays', return_value=[]), \
         patch('insight_capture.runtime.mapping.looper_node.query_arrays', return_value=[]):
        worker = threading.Thread(target=LooperNode.run, args=(node,)); worker.start()
        try:
            put('head'); assert positions() == [1, 2, 3]
            assert {s.map_revision for s in node.cameras.values()} == {12}
            put('insight3_a'); assert positions() == [1, 10, 3]
            put('insight3_b'); assert positions() == [1, 10, 20]
            block_head.set()
            put('head')
            node.generation += 1
            release_head.set()
            time.sleep(.03)
            assert positions() == [1, 10, 20], 'old generation committed after reset'
        finally:
            node.stop.set(); worker.join(timeout=2)
            assert not worker.is_alive()
    print('atomic map correction, geometry ordering and in-flight reset isolation: passed')


if __name__ == '__main__':
    main()
