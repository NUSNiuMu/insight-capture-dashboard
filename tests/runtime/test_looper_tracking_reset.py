"""Replay VIO lifecycle events through the actual ROS mapper in its runtime image."""
from collections import deque
from functools import partial
import threading
from types import SimpleNamespace as S

import numpy as np
import pytest
pytest.importorskip("rclpy")
from geometry_msgs.msg import PoseStamped

from insight_capture.runtime.diagnostics import DiagnosticEvents
from insight_capture.runtime.mapping.looper_node import CameraState, LooperNode
from insight_capture.runtime.mapping.rig import LatestViewQueue
from insight_capture.runtime.mapping.synchronization import StereoPairSynchronizer

RESET_STATES = ["NOT_INITED", "NO_CALIBRATION", "RESTARTING", "STOPPED"]

class Sink:
    def __init__(self):
        self.values = []
    def publish(self, value):
        self.values.append(value)
    def sendTransform(self, value):
        pass

def message(seconds, x=0.):
    value = PoseStamped()
    value.header.stamp.sec = seconds
    value.pose.position.x = x
    value.pose.orientation.w = 1.
    return value

@pytest.fixture
def mapper():
    node = object.__new__(LooperNode)
    node.lock, node.processing = threading.RLock(), threading.Lock()
    node.head, node.names = "head", ("head", "wrist")
    node.cameras = {name: CameraState() for name in node.names}
    node.ready, node.generation, node.session_id = True, 0, "fixture"
    node.now = node.last_map_source_ns = 10_000_000_000
    node.last_reset_wall_ns, node.last_geometry = 0, 0.
    node.last_pose_tick, node.head_raw_correction = None, np.eye(4)
    node.pose_corrections = deque(maxlen=1000)
    node.work, node.prepared = LatestViewQueue(node.names), LatestViewQueue(node.names)
    node.sync = StereoPairSynchronizer(tolerance_ns=2_000_000)
    node.frames = {name: name + "_global" for name in node.names}
    node.pose_publishers = {name: Sink() for name in node.names}
    node.status_publishers = {name: Sink() for name in node.names}
    node.tf_broadcaster = Sink()
    node.feature_publisher, node.reference_points, node.reference_image = Sink(), Sink(), Sink()
    node.backend_calls = []
    node.backend = S(request=lambda *a, **k: node.backend_calls.append((a, k)))
    node.stats = {"state": "mapping", "map_point_count": 20}
    node.get_clock = lambda: S(now=lambda: S(nanoseconds=node.now))
    logger = S(info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None)
    node.get_logger = lambda: logger
    node.events = DiagnosticEvents(logger)
    for name, state in node.cameras.items():
        state.center, state.geometry_id = np.eye(4), ("unchanged_geometry",)
        state.tracking = "TRACKING"
        state.clock.offset_ns, state.clock.samples = 0, 100
        state.last_raw_source_ns = state.last_raw_received_ns = node.now
        value = message(10, 2. if name == "wrist" else 0.)
        node.on_frame_pose(name, value.header, value.pose)
        correction, native = np.eye(4), np.eye(4)
        correction[0, 3] = 10. if name == "wrist" else 0.
        native[0, 3] = value.pose.position.x
        state.fusion.observe(correction, native, node.now, node.now, verified=True)
        state.correction = state.fusion.emitted.copy()
    node.publish_poses()
    assert [p.pose.position.x for p in node.pose_publishers["wrist"].values] == [12.]
    for sink in node.pose_publishers.values():
        sink.values.clear()
    return node

def advance(node, *, wrist_x=0., seconds=21):
    # The wall/source clocks remain continuous across the SDK restart.
    for index in range(100):
        node.now = (seconds - 1) * 1_000_000_000 + index * 10_000_000
        for name in node.names:
            value = message(0)
            value.header.stamp.sec, value.header.stamp.nanosec = divmod(node.now, 1_000_000_000)
            node.on_vio(name, value)
    node.now = seconds * 1_000_000_000
    for name in node.names:
        value = message(seconds, wrist_x if name == "wrist" else 0.)
        node.on_vio(name, value)
        node.on_frame_pose(name, value.header, value.pose)

@pytest.mark.parametrize("name", ["head", "wrist"])
@pytest.mark.parametrize("status", RESET_STATES)
def test_sdk_restart_never_reuses_old_common_map_without_a_new_match(mapper, name, status):
    node = mapper
    node.on_tracking(name, status)
    node.on_tracking(name, "TRACKING")
    advance(node)
    node.publish_poses()
    state = node.cameras[name]
    assert state.clock.ready
    assert state.correction is None
    assert state.tracking_epoch == 1
    assert not node.pose_publishers[name].values
    if name == "head":
        assert node.generation == 1
        assert all(s.correction is None for s in node.cameras.values())
        assert len(node.backend_calls) == 1
    else:
        assert node.generation == 0
        assert node.cameras["head"].correction is not None
        assert node.pose_publishers["head"].values

@pytest.mark.parametrize("name", ["head", "wrist"])
def test_repeated_restart_status_does_not_repeat_map_reset(mapper, name):
    mapper.on_tracking(name, "RESTARTING")
    mapper.on_tracking(name, "RESTARTING")
    mapper.on_tracking(name, "NOT_INITED")
    assert mapper.cameras[name].tracking_epoch == 1
    assert len(mapper.backend_calls) == (1 if name == "head" else 0)

@pytest.mark.parametrize("status", ["TRACKING_STATIC", "TRACKING_LOST", "DATA_LOST"])
def test_nonreset_tracking_changes_preserve_the_existing_coordinate_frame(mapper, status):
    mapper.on_tracking("wrist", status)
    mapper.on_tracking("wrist", "TRACKING")
    advance(mapper, wrist_x=2.)
    mapper.publish_poses()
    assert mapper.cameras["wrist"].tracking_epoch == 0
    assert [p.pose.position.x for p in mapper.pose_publishers["wrist"].values] == [12.]

@pytest.mark.parametrize("name", ["head", "wrist"])
def test_startup_without_a_previous_vio_session_does_not_reset_a_map(mapper, name):
    mapper.cameras[name] = CameraState()
    mapper.on_tracking(name, "NOT_INITED")
    assert mapper.cameras[name].tracking_epoch == 0
    assert not mapper.backend_calls

@pytest.mark.parametrize("name", ["head", "wrist"])
def test_inflight_old_epoch_result_cannot_restore_invalidated_calibration(mapper, name):
    item = S(view=name, generation=mapper.generation,
             tracking_epoch=mapper.cameras[name].tracking_epoch, stamp_ns=mapper.now)
    mapper.on_tracking(name, "RESTARTING")
    result = {"mapped": True, "localized": True, "verified_relocalization": True,
              "map_from_imu": np.eye(4).tolist(), "map_revision": 7,
              "landmarks": 20, "wrist_updates": [], "localization_ready": True,
              "keyframes": 3, "raw_landmarks": 20}
    assert mapper.commit_result(item, result, np.eye(4)) is False

def test_wrist_reacquires_using_only_a_new_geometric_match(mapper):
    mapper.on_tracking("wrist", "RESTARTING")
    mapper.on_tracking("wrist", "TRACKING")
    advance(mapper)
    mapper.publish_poses()
    assert not mapper.pose_publishers["wrist"].values
    state = mapper.cameras["wrist"]
    correction = np.eye(4)
    correction[0, 3] = 12.
    assert mapper.update_correction(state, correction, mapper.now,
                                   partial(state.fusion.observe, verified=True))
    advance(mapper, seconds=22)
    mapper.publish_poses()
    assert state.tracking_epoch == 1
    assert [p.pose.position.x for p in mapper.pose_publishers["wrist"].values] == [12.]


@pytest.mark.parametrize("name", ["head", "wrist"])
def test_status_exposes_the_pose_invalidation_boundary(mapper, name):
    import json
    mapper.on_tracking(name, "RESTARTING")
    mapper.publish_status()
    value = json.loads(mapper.status_publishers[name].values[-1].data)
    assert value["tracking_epoch"] == 1
    assert value["last_pose_reset_wall_ns"] == mapper.now
    assert value["localized"] is False

@pytest.mark.parametrize("name", ["head", "wrist"])
def test_clock_resynchronization_reports_a_new_boundary_without_discarding_spatial_calibration(mapper, name):
    previous = mapper.cameras[name].correction
    mapper.now = 20_000_000_000
    mapper.on_vio(name, message(15))
    state = mapper.cameras[name]
    assert state.tracking_epoch == 1
    assert state.last_pose_reset_wall_ns == mapper.now
    assert state.correction is previous
    assert mapper.generation == 0

def test_geometry_change_reports_a_new_pose_boundary(mapper):
    state = mapper.cameras["wrist"]
    state.info = S(width=640, height=480, header=S(frame_id="wrist_left"),
                   p=[500.,0.,320.,0.,0.,500.,240.,0.,0.,0.,1.,0.],
                   r=np.eye(3).ravel().tolist())
    mapper.inputs = {"wrist":{"imu_frame":"wrist_imu", "center_left_frame":"left", "center_right_frame":"right"}}
    mapper.tf_buffer = S(can_transform=lambda *a:True)
    mapper.lookup = lambda *a:np.eye(4)
    mapper.now = 15_000_000_000
    assert mapper.configure("wrist")
    assert state.tracking_epoch == 1
    assert state.last_pose_reset_wall_ns == mapper.now
    assert state.correction is None


@pytest.mark.parametrize('wall_start_ns', [5_000_000_000, 1_794_840_000_000_000_000])
def test_wall_clock_correction_restarts_common_ticks_without_replaying_missed_time(mapper, wall_start_ns):
    class BoundedTicks(deque):
        def append(self, value):
            self.additions += 1
            assert self.additions <= 3, 'Unbounded catch-up across a wall-clock correction'
            super().append(value)
    ticks = BoundedTicks(mapper.pose_corrections, maxlen=1000)
    ticks.additions = 0
    mapper.pose_corrections = ticks
    previous_corrections = {n:s.correction.copy() for n,s in mapper.cameras.items()}
    for index in range(101):
        mapper.now = wall_start_ns + index * 10_000_000
        for name in mapper.names:
            msg = message(0, 2. if name == 'wrist' else 0.)
            msg.header.stamp.sec, msg.header.stamp.nanosec = divmod(
                10_010_000_000 + index * 10_000_000, 1_000_000_000)
            mapper.on_vio(name, msg)
            mapper.on_frame_pose(name, msg.header, msg.pose)
    mapper.publish_poses()
    assert ticks.additions == 1
    for name, state in mapper.cameras.items():
        assert state.clock.ready and state.tracking_epoch == 1
        assert np.allclose(state.correction, previous_corrections[name])
        poses = mapper.pose_publishers[name].values
        assert poses and abs(poses[-1].header.stamp.sec - mapper.now // 1_000_000_000) <= 1
    assert mapper.pose_publishers['wrist'].values[-1].pose.position.x == pytest.approx(12.)
