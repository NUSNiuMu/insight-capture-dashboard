"""Run the real installer with isolated command boundaries, never a live Docker daemon."""

import asyncio
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from insight_capture.api.routes.recording import RecordingRoutes
from insight_capture.runtime import upgrade


ROOT = Path(__file__).resolve().parents[1]
IDLE = {"recording": False, "merge_state": "idle", "upgrade_lock_supported": True}


@pytest.fixture
def installation(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/calibration.json").write_text('{"site": "unchanged"}\n')
    (tmp_path / "rosbags").mkdir()
    (tmp_path / "rosbags/kept.mcap").write_bytes(b"protected fixture data")
    (tmp_path / ".env").write_text("INSIGHT_VERSION=v2.1.0\nSITE_SETTING=preserve\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "images.tar").touch()
    (tmp_path / "bin").mkdir()
    image = tmp_path / "fixture-image"
    for name in ("deploy", "scripts", "insight_capture/runtime", "tools/diagnostics",
                 "config/devices", "native_dashboard", "native"):
        (image / name).mkdir(parents=True)
    for name in ("insight_capture/__init__.py", "insight_capture/runtime/discovery.py",
                 "insight_capture/runtime/camera_profiles.py", "tools/diagnostics/sync_camera_restart.py",
                 "native_dashboard/run.sh", "native/insight-native"):
        (image / name).touch()
    (image / "deploy/docker-compose.yml").write_text("services: {}\n")
    with tarfile.open(image / "native/qt-runtime.tar", "w"):
        pass
    docker = tmp_path / "bin/docker"
    docker.write_text('''#!/usr/bin/env python3
import fcntl,json,os,pathlib,shutil,subprocess,sys
p=pathlib.Path(os.environ['QA_UPGRADE_ROOT']); a=sys.argv[1:]
with (p/'commands.jsonl').open('a') as f: f.write(json.dumps(a)+'\\n')
if a==['compose','version']: sys.exit(0)
if a and a[0]=='load':
    if (p/'config').exists():
        with (p/'config/.capture-upgrade.lock').open('rb') as f:
            try: fcntl.flock(f,fcntl.LOCK_SH|fcntl.LOCK_NB)
            except BlockingIOError: (p/'locked-during-load').touch()
    (p/'loaded').touch(); print('Loaded image: insight-dashboard:v2.1.1'); sys.exit(0)
if a[:2]==['image','inspect']: print('sha256:test'); sys.exit(0)
if a and a[0]=='ps':
    if os.environ.get('QA_BACKEND_RUNNING')=='1': print('qa-backend')
    sys.exit(0)
if a and a[0]=='inspect':
    print(os.environ.get('QA_CONTAINER_STATE','running 321')); sys.exit(0)
if a and a[0]=='run':
    if '/candidate/deploy/release_compose.py' in a:
        candidate=next(x.split(':/candidate')[0] for x in a if x.endswith(':/candidate'))
        sys.exit(subprocess.run([os.environ['QA_PYTHON'], str(pathlib.Path(candidate)/'deploy/release_compose.py'),
                                 'stage', candidate]).returncode)
    if '-c' in a: sys.exit(0)
if a[:1]==['compose'] and 'config' in a:
    print(json.dumps({'services': {'insight-dashboard': {
        'image': 'insight-dashboard:' + os.environ['INSIGHT_VERSION'],
        'environment': {'DASHBOARD_PORT': '8875'}}}}))
    sys.exit(0)
if 'down' in a: (p/'service-stopped').touch(); sys.exit(0)
if a and a[0]=='create' and not (p/'config').exists(): print('qa-seed'); sys.exit(0)
if a and a[0]=='cp' and a[1].startswith('qa-seed:'):
    (p/'config').mkdir(); (p/'config/seeded.json').write_text('{}'); sys.exit(0)
if a==['rm','qa-seed']: sys.exit(0)
if a[:1]==['create']: print('qa-runtime'); sys.exit(0)
if a==['rm','qa-runtime']: sys.exit(0)
if a[:1]==['cp'] and a[1].startswith('qa-runtime:'):
    relative=a[1].split(':',1)[1].removeprefix('/workspaces/insight_capture/')
    source=p/'fixture-image'/('native' if relative=='/opt/insight/native' else relative)
    destination=pathlib.Path(a[2])
    if source.is_dir(): shutil.copytree(source,destination)
    else: shutil.copy2(source,destination)
    sys.exit(0)
# Stop at host migration. No command ever reaches real Docker or sudo.
sys.exit(73)
''')
    docker.chmod(0o755)
    curl = tmp_path / "bin/curl"
    curl.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
p=pathlib.Path(os.environ['QA_UPGRADE_ROOT'])
state=json.loads(os.environ['QA_AFTER' if (p/'loaded').exists() else 'QA_BEFORE'])
if state=='unreachable': sys.exit(7)
if state=='malformed': print('{broken'); sys.exit(0)
print(json.dumps(state))
''')
    curl.chmod(0o755)
    return tmp_path


def run_installer(root, before=IDLE, after=IDLE, *, running=True, force=False, container_state='running 321'):
    env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}",
               QA_UPGRADE_ROOT=str(root), QA_BEFORE=json.dumps(before),
               QA_AFTER=json.dumps(after), QA_BACKEND_RUNNING=str(int(running)),
               QA_PYTHON=sys.executable, QA_CONTAINER_STATE=container_state)
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/update.sh"), "--install-dir", str(root),
         *(["--force"] if force else []), str(root / "images.tar")],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert (root / "rosbags/kept.mcap").read_bytes() == b"protected fixture data"
    return result


def assert_old_install_kept(root, result):
    assert result.returncode == 1, result.stdout + result.stderr
    assert not (root / "service-stopped").exists()
    assert (root / ".env").read_text() == "INSIGHT_VERSION=v2.1.0\nSITE_SETTING=preserve\n"
    assert (root / "config/calibration.json").read_text() == '{"site": "unchanged"}\n'


@pytest.mark.parametrize("state", [
    {**IDLE, "recording": True},
    {**IDLE, "merge_state": "finalizing"},
    {**IDLE, "merge_state": "merging"},
    {**IDLE, "operation_in_progress": True},
    {**IDLE, "current_take": {"state": "preparing"}},
])
def test_busy_initial_state_never_loads_or_stops(installation, state):
    result = run_installer(installation, before=state)
    assert_old_install_kept(installation, result)
    assert not (installation / "loaded").exists()


def test_legacy_capture_start_during_image_load_is_rechecked(installation):
    result = run_installer(installation,
                           before={"recording": False, "merge_state": "idle"},
                           after={"recording": True, "merge_state": "idle"})
    assert_old_install_kept(installation, result)
    assert (installation / "loaded").exists()
    assert "predates upgrade/start locking" in result.stdout


@pytest.mark.parametrize("reply", ["unreachable", "malformed", {}, []])
@pytest.mark.parametrize("after_load", [False, True])
def test_unknown_running_backend_never_stops(installation, reply, after_load):
    result = run_installer(installation, before=IDLE if after_load else reply,
                           after=reply)
    assert_old_install_kept(installation, result)
    assert "status is unavailable" in result.stderr


def test_idle_upgrade_holds_lock_until_exit(installation):
    result = run_installer(installation)
    assert result.returncode == 73  # Intentional fake Docker boundary.
    assert (installation / "locked-during-load").exists()
    assert (installation / "service-stopped").exists()
    with (installation / "config/.capture-upgrade.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert "INSIGHT_VERSION=v2.1.1" in (installation / ".env").read_text()


def test_retries_previous_crash_loop_without_force(installation):
    result = run_installer(installation, before="unreachable", after="unreachable",
                           container_state="restarting 0")
    assert result.returncode == 73, result.stdout + result.stderr
    assert "has no live process" in result.stdout
    assert (installation / "service-stopped").exists()


def test_stages_site_env_and_removes_old_code_mounts(installation):
    env = installation / ".env"
    env.chmod(0o444)
    override = installation / "docker-compose.override.yml"
    override.write_text("""services:
  insight-dashboard:
    volumes:
      - ./.camera-time-check/distributed.py:/workspaces/insight_capture/insight_capture/runtime/recording/distributed.py
      - type: bind
        source: ./.old-preview/preview_config.py
        target: /workspaces/insight_capture/insight_capture/media/preview_config.py
      - ./.old-preview/mobile.js:/workspaces/insight_capture/web_dashboard/dist/static/mobile.js
      - ./rosbags:/mnt/insight-recordings
      - /usr/bin/kmod:/usr/sbin/lsmod
""")
    original = override.read_bytes()
    result = run_installer(installation)
    assert result.returncode == 73, result.stdout + result.stderr
    assert env.read_text() == "SITE_SETTING=preserve\nINSIGHT_VERSION=v2.1.1\n"
    assert env.stat().st_mode & 0o777 == 0o444
    assert "old-preview" not in override.read_text()
    assert "camera-time-check" not in override.read_text()
    assert "./rosbags:/mnt/insight-recordings" in override.read_text()
    assert "/usr/bin/kmod:/usr/sbin/lsmod" in override.read_text()
    previous, = installation.glob(".update.*/previous")
    assert (previous / override.name).read_bytes() == original
    assert "INSIGHT_VERSION=v2.1.0" in (previous / ".env").read_text()


def test_first_install_still_seeds_config_before_lock(installation):
    (installation / "config/calibration.json").unlink()
    (installation / "config").rmdir()
    result = run_installer(installation, before="unreachable", after="unreachable", running=False)
    assert result.returncode == 73
    assert (installation / "config/seeded.json").exists()
    assert (installation / "config/.capture-upgrade.lock").exists()


def test_force_remains_an_explicit_recording_interrupt_override(installation):
    busy = {**IDLE, "recording": True}
    result = run_installer(installation, before=busy, after=busy, force=True)
    assert result.returncode == 73
    assert (installation / "service-stopped").exists()
    assert "explicitly permits interrupting" in result.stdout


def test_inflight_start_excludes_real_installer(installation, monkeypatch):
    monkeypatch.setattr(upgrade, "LOCK_PATH", installation / "config/.capture-upgrade.lock")
    with upgrade.recording_start_lock():
        result = run_installer(installation)
    assert_old_install_kept(installation, result)
    assert "recording start or another upgrade" in result.stderr
    assert not (installation / "loaded").exists()


def test_installer_lock_rejects_start_before_preflight_or_take(installation, monkeypatch):
    lock_path = installation / "config/.capture-upgrade.lock"
    lock_path.touch()
    monkeypatch.setattr(upgrade, "LOCK_PATH", lock_path)
    route = RecordingRoutes(SimpleNamespace())
    route._start_with_preview_paused = AsyncMock(return_value="started")
    async def scenario():
        with lock_path.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="系统正在升级"):
                await route._start_with_preflight(topics=None, bag_name=None)
        route._start_with_preview_paused.assert_not_awaited()
        assert not route.busy
        assert await route._start_with_preflight(topics=None, bag_name=None) == "started"
    asyncio.run(scenario())



def test_poweroff_lock_blocks_manual_merge_admission(installation, monkeypatch):
    lock_path = installation / "config/.capture-upgrade.lock"
    lock_path.touch()
    monkeypatch.setattr(upgrade, "LOCK_PATH", lock_path)
    called = []
    manager = SimpleNamespace(start_merge=lambda session: called.append(session) or {})
    routes = RecordingRoutes(SimpleNamespace(recording_manager=manager))
    request = SimpleNamespace(
        can_read_body=True, json=AsyncMock(return_value={"session_id": "fixture"})
    )

    async def scenario():
        with lock_path.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="准备关机"):
                await routes._handle_saved_recording_merge(request)
        assert called == []
        assert (await routes._handle_saved_recording_merge(request)).status == 202
        assert called == ["fixture"]

    asyncio.run(scenario())


def test_start_holds_lock_through_async_preflight(installation, monkeypatch):
    lock_path = installation / "config/.capture-upgrade.lock"
    monkeypatch.setattr(upgrade, "LOCK_PATH", lock_path)
    route = RecordingRoutes(SimpleNamespace())
    async def prepare(**kwargs):
        await asyncio.sleep(0)
        with lock_path.open("rb") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise ValueError("preflight failed")
    route._start_with_preview_paused = prepare
    async def scenario():
        with pytest.raises(ValueError, match="preflight failed"):
            await route._start_with_preflight(topics=None, bag_name=None)
        with lock_path.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(scenario())


def test_disconnected_request_keeps_lock_until_start_finishes(installation, monkeypatch):
    lock_path = installation / "config/.capture-upgrade.lock"
    monkeypatch.setattr(upgrade, "LOCK_PATH", lock_path)
    route = RecordingRoutes(SimpleNamespace())
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        async def prepare(**kwargs):
            entered.set()
            await finish.wait()
            return "started"
        route._start_with_preview_paused = prepare
        request = asyncio.create_task(route._start_with_preflight(topics=None, bag_name=None))
        await entered.wait()
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0)
        try:
            assert route.busy
            assert not request.done()
            with lock_path.open("rb") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert not route.busy
        with lock_path.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    asyncio.run(scenario())
