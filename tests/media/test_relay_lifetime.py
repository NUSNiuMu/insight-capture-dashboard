"""The relay must not retain ports after its owning worker is killed."""
import json
import os
from pathlib import Path
import platform
import select
import signal
import subprocess
import sys
import time

import pytest
import insight_capture
from insight_capture.media.webrtc import WebRtcStreams


@pytest.mark.skipif(platform.machine() not in {'aarch64', 'arm64'}, reason='Bundled go2rtc is ARM64')
def test_real_relay_exits_when_its_owner_is_killed():
    source = Path(WebRtcStreams._start_service.__code__.co_filename).read_text()
    canonical = str(Path(insight_capture.__file__).resolve().parent / 'media/webrtc.py')
    body = '''
import asyncio,importlib,json,os,socket,sys,types
importlib.import_module('insight_capture.media')
module=types.ModuleType('insight_capture.media.webrtc')
module.__package__='insight_capture.media';module.__file__=CANONICAL
sys.modules[module.__name__]=module;exec(compile(SOURCE,module.__file__,'exec'),module.__dict__)
async def main():
 with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
 service=module.WebRtcStreams({'sources':[],'overview':None},min(port,64000),lambda *a:None)
 await service.start(None);await service.service('127.0.0.1')
 print(json.dumps({'relay_pid':next(iter(service.services.values()))[0].pid}),flush=True)
 while True:await asyncio.sleep(1)
asyncio.run(main())
'''.replace('CANONICAL', repr(canonical)).replace('SOURCE', repr(source))
    parent = subprocess.Popen([sys.executable, '-u', '-c', body], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert select.select([parent.stdout], [], [], 8)[0], 'Relay startup did not report child'
        line = parent.stdout.readline()
        assert line, parent.stderr.read()[:2500]
        relay = json.loads(line)['relay_pid']
        parent.kill()
        parent.wait(timeout=5)

        def alive(pid):
            try:
                return Path('/proc/' + str(pid) + '/stat').read_text().split()[2] != 'Z'
            except FileNotFoundError:
                return False

        for _ in range(40):
            if not alive(relay):
                break
            time.sleep(.05)
        assert not alive(relay), 'go2rtc outlived its owning worker'
    finally:
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait(timeout=5)
