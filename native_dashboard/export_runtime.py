#!/usr/bin/env python3
"""Export Qt libraries without replacing JetPack's GStreamer or NVIDIA stack."""
import glob
import pathlib
import subprocess
import sys
import tarfile

root = pathlib.Path('/usr/lib/aarch64-linux-gnu')
paths = set(glob.glob(str(root / 'libQt6*.so*')))
paths.add(str(root / 'qt6'))
for package in ('libb2-1', 'libmd4c0', 'libdouble-conversion3', 'libassimp5',
                'libdraco4', 'libxcb-icccm4', 'libxcb-image0', 'libxcb-keysyms1',
                'libxcb-render-util0', 'libxcb-xinput0', 'libxcb-xkb1',
                'libxkbcommon-x11-0', 'libmng2', 'libwebpdemux2'):
    for name in subprocess.check_output(['dpkg-query', '-L', package], text=True).splitlines():
        if name.startswith(str(root) + '/') and '.so' in name:
            paths.add(name)
with tarfile.open(sys.argv[1], 'w') as archive:
    for name in sorted(paths):
        archive.add(name, arcname=name.lstrip('/'))
