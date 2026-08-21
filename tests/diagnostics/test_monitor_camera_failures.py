from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor_camera_failures import (  # noqa: E402
    Condition,
    KernelEventCorrelator,
    SustainedEventTracker,
    _is_dashboard_runtime_command,
    derive_conditions,
    map_probes_by_camera_identity,
)


def _snapshot(*, stale: bool = False, rx_delta: int = 1000) -> dict:
    return {
        "expected_ros_domain_id": 20,
        "dashboard": {
            "ok": True,
            "cameras": {
                "insight3_a": {
                    "name": "insight3_a",
                    "stale": stale,
                    "input_age_sec": None if stale else 0.02,
                    "version": 10,
                    "webrtc_stats": {"input_fps": 29.8},
                }
            },
        },
        "cameras": {
            "insight3_a": {
                "http_ok": True,
                "ros_domain_id": 20,
                "expected_fps": 30.0,
                "interface": {
                    "name": "enx020000000a01",
                    "present": True,
                    "carrier": 1,
                    "rx_bytes_delta": rx_delta,
                },
            }
        },
    }


def test_stale_image_with_live_http_and_usb_traffic_is_attributed_to_image_path() -> None:
    conditions = derive_conditions(_snapshot(stale=True, rx_delta=12000))

    fault = conditions["camera.insight3_a.image_stale"]
    assert fault.cause == "image_publisher_or_dds_large_payload_stalled"
    assert fault.evidence["camera_http_ok"] is True


def test_stale_image_without_usb_traffic_is_attributed_to_ros_data_path() -> None:
    conditions = derive_conditions(_snapshot(stale=True, rx_delta=0))

    assert (
        conditions["camera.insight3_a.image_stale"].cause
        == "camera_ros_data_path_stalled"
    )


def test_vio_calibration_recording_ignores_expected_rectified_staleness() -> None:
    snapshot = _snapshot(stale=True, rx_delta=12000)
    snapshot["recording"] = {
        "recording": True,
        "recording_mode": "vio_calibration",
    }

    conditions = derive_conditions(snapshot)

    assert "camera.insight3_a.image_stale" not in conditions


def test_raw_only_mode_is_detected_before_recording_from_native_vio() -> None:
    snapshot = _snapshot(stale=True, rx_delta=12000)
    snapshot["dashboard"]["cameras"]["insight3_a"]["native_vio_fresh"] = True
    snapshot["dashboard"]["cameras"]["insight3_b"] = {
        "name": "insight3_b",
        "stale": True,
        "input_age_sec": None,
        "native_vio_fresh": True,
        "version": 0,
    }
    snapshot["cameras"]["insight3_b"] = {
        "http_ok": True,
        "ros_domain_id": 20,
        "expected_fps": 30.0,
        "interface": {
            "name": "enx020000001401",
            "present": True,
            "carrier": 1,
            "rx_bytes_delta": 12000,
        },
    }

    conditions = derive_conditions(snapshot)

    assert "camera.insight3_a.image_stale" not in conditions
    assert "camera.insight3_b.image_stale" not in conditions


def test_multiple_missing_interfaces_identify_shared_usb_failure() -> None:
    snapshot = _snapshot()
    snapshot["cameras"]["insight3_b"] = {
        "http_ok": False,
        "interface": {"name": "enx020000001401", "present": False},
    }
    snapshot["cameras"]["insight3_a"]["interface"] = {
        "name": "enx020000000a01",
        "present": False,
    }

    conditions = derive_conditions(snapshot)

    assert conditions["usb.shared_link_failure"].cause == (
        "shared_usb_hub_power_or_controller_reset"
    )
    assert "camera.insight3_a.usb_link_down" not in conditions


def test_domain_mismatch_has_specific_cause() -> None:
    snapshot = _snapshot()
    snapshot["cameras"]["insight3_a"]["ros_domain_id"] = 21

    conditions = derive_conditions(snapshot)

    assert conditions["camera.insight3_a.ros_domain_mismatch"].cause == (
        "ros_domain_configuration_mismatch"
    )


def test_low_fps_is_an_early_warning() -> None:
    snapshot = _snapshot()
    snapshot["dashboard"]["cameras"]["insight3_a"]["webrtc_stats"] = {
        "input_fps": 12.0
    }

    conditions = derive_conditions(snapshot)

    fault = conditions["camera.insight3_a.fps_degraded"]
    assert fault.severity == "warning"
    assert fault.sustain_sec == 10.0


def test_duplicate_dashboard_processes_are_reported() -> None:
    snapshot = _snapshot()
    snapshot["dashboard_runtime_pids"] = [101, 202]

    conditions = derive_conditions(snapshot)

    fault = conditions["dashboard.runtime_process_count"]
    assert fault.cause == "overlapping_or_duplicate_dashboard_backends"
    assert fault.evidence["pids"] == [101, 202]


def test_camera_identity_dynamically_follows_changed_ip() -> None:
    assignments, conflicts = map_probes_by_camera_identity(
        ["insight3_a", "insight3_b"],
        {
            "169.254.10.1": {
                "ip": "169.254.10.1",
                "http_ok": True,
                "camera_config": {"cameraNamespace": "insight3_b"},
            },
            "169.254.20.1": {
                "ip": "169.254.20.1",
                "http_ok": False,
            },
        },
    )

    assert assignments["insight3_b"]["ip"] == "169.254.10.1"
    assert "insight3_a" not in assignments
    assert conflicts == {}


def test_duplicate_live_camera_identity_is_critical() -> None:
    snapshot = _snapshot()
    snapshot["camera_identity_conflicts"] = {
        "insight3_a": ["169.254.10.1", "169.254.40.1"]
    }

    conditions = derive_conditions(snapshot)

    fault = conditions["camera.insight3_a.duplicate_identity"]
    assert fault.severity == "critical"
    assert fault.cause == "duplicate_camera_namespace_on_multiple_live_peers"


def test_missing_dynamic_identity_is_reported_without_assuming_old_ip() -> None:
    snapshot = _snapshot()
    snapshot["cameras"]["insight3_a"] = {
        "name": "insight3_a",
        "discovered": False,
        "candidate_peers": {"169.254.10.1": {"reported_camera": "insight3_b"}},
    }

    conditions = derive_conditions(snapshot)

    assert conditions["camera.insight3_a.not_discovered"].cause == (
        "camera_updating_powered_off_or_not_on_any_discovered_peer"
    )


def test_runtime_pid_filter_excludes_docker_init_wrapper() -> None:
    wrapper = (
        b"/sbin/docker-init\0--\0/entrypoint.sh\0python3\0-u\0-m\0"
        b"insight_capture.runtime.app\0"
    )
    backend = b"python3\0-u\0-m\0insight_capture.runtime.app\0"

    assert _is_dashboard_runtime_command(wrapper) is False
    assert _is_dashboard_runtime_command(backend) is True


def test_tracker_emits_only_sustained_start_and_resolution() -> None:
    tracker = SustainedEventTracker(default_sustain_sec=3.0)
    condition = Condition("critical", "test", "test")

    assert tracker.update({"fault": condition}, 10.0) == []
    assert tracker.update({"fault": condition}, 12.9) == []
    started = tracker.update({"fault": condition}, 13.0)
    assert [(event, code) for event, code, _condition, _age in started] == [
        ("started", "fault")
    ]
    assert tracker.update({"fault": condition}, 20.0) == []
    resolved = tracker.update({}, 21.0)
    assert [(event, code) for event, code, _condition, _age in resolved] == [
        ("resolved", "fault")
    ]


def test_kernel_correlator_marks_multi_device_disconnect_cluster() -> None:
    correlator = KernelEventCorrelator(cluster_window_sec=5.0)
    first = correlator.observe(
        "usb 2-1.2: USB disconnect, device number 4", 10.0
    )
    second = correlator.observe(
        "usb 2-1.1: USB disconnect, device number 3", 12.0
    )

    assert [event["event"] for event in first] == ["usb_camera_disconnect"]
    assert [event["event"] for event in second] == [
        "usb_camera_disconnect",
        "usb_disconnect_cluster",
    ]
    assert second[-1]["cause"] == "shared_usb_hub_power_or_controller_reset"
