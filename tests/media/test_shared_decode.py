import os
import socket
from types import SimpleNamespace

import pytest

from insight_capture.media import shared_decode


def test_shared_socket_owner_cleans_stale_socket_without_overriding_live_owner(tmp_path, monkeypatch):
    root = tmp_path / "nvmm"
    monkeypatch.setattr(shared_decode, "ROOT", root)
    monkeypatch.setattr(shared_decode.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=os.getegid()))
    owner = shared_decode.SharedDecodeSocket("left", 1200, 1600)
    assert (owner.width, owner.height) == (540, 720)
    try:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(owner.path))
            owner.allow_client()
            assert shared_decode.ready_preview_socket("left") == owner.path
            assert not owner.has_viewers()
            demand = shared_decode.hold_preview_demand(owner.path)
            try:
                owner._demand_check = 0
                assert owner.has_viewers()
            finally:
                os.close(demand)
            owner._demand_check = 0
            assert not owner.has_viewers()
            with pytest.raises(BlockingIOError):
                shared_decode.SharedDecodeSocket("left", 1200, 1600)
            assert owner.path.is_socket()
    finally:
        owner.close()
    assert shared_decode.ready_preview_socket("left") is None


def test_shared_socket_rejects_names_outside_its_directory():
    with pytest.raises(ValueError):
        shared_decode.preview_socket("../other")
    assert shared_decode.shared_decode_sources({}) == frozenset()
