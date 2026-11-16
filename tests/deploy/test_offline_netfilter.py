"""Selector policy only; kernel and bootstrap integration are separate checks."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'deploy/offline/select_iptables_backend.sh'
MOCK = """import json
import os
from pathlib import Path
import subprocess
import sys

path = Path(os.environ['SELECTOR_CASE'])
state = json.loads(path.read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state['calls'].append([name, *args])
path.write_text(json.dumps(state))

if name == 'unshare':
    sys.exit(subprocess.run(args[args.index('--') + 1:]).returncode)
if name == 'iptables' and '--version' in args:
    print('iptables v1.8.7 (' + state['backend'] + ')')
    sys.exit(0)
if name == 'update-alternatives':
    if args[0] == '--set':
        state['selected'].append(args[1])
        path.write_text(json.dumps(state))
        print('selected alternative')
    sys.exit(0)

works = state['legacy_works'] if name.endswith('-legacy') else state['nft_works']
sys.exit(0 if works else 4)
"""

@pytest.mark.parametrize('backend,nft_works,legacy_works,available,ipv6,expected,selected', [
    ('nf_tables', False, True, True, True, 0, ['iptables', 'ip6tables']),
    ('nf_tables', False, True, True, False, 0, ['iptables']),
    ('nf_tables', True, False, True, True, 0, []),
    ('legacy', False, True, True, True, 0, []),
    ('nf_tables', False, False, True, True, 1, []),
    ('nf_tables', False, False, False, True, 1, []),
])
def test_backend_selection_preserves_working_configuration(
    tmp_path, backend, nft_works, legacy_works, available, ipv6, expected, selected
):
    state = tmp_path / 'state.json'
    state.write_text(json.dumps(dict(backend=backend, nft_works=nft_works,
                                    legacy_works=legacy_works, calls=[], selected=[])))
    commands = ['iptables', 'unshare', 'update-alternatives']
    if available:
        commands.append('iptables-legacy')
    if ipv6:
        commands.append('ip6tables-legacy')
    for name in commands:
        p = tmp_path / name
        p.write_text('#!' + sys.executable + '\n' + MOCK)
        p.chmod(0o755)
    shell = tmp_path / 'sh'
    shell.write_text('#!/bin/sh\nexec /bin/sh "$@"\n')
    shell.chmod(0o755)
    r = subprocess.run(['/bin/bash', str(SCRIPT)], text=True, capture_output=True,
                       env={**os.environ, 'PATH': str(tmp_path), 'SELECTOR_CASE': str(state)},
                       timeout=10)
    result = json.loads(state.read_text())
    assert r.returncode == expected
    assert result['selected'] == selected
    assert r.stdout.strip() == ('legacy' if selected else '')
    # Every rule probe must enter the disposable namespace, not the host.
    for call in result['calls']:
        if call[0] == 'unshare':
            assert call[1:3] == ['--net', '--']
    if backend == 'legacy':
        assert not any(c[0] == 'unshare' for c in result['calls'])
