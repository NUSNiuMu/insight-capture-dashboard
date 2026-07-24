#!/usr/bin/env bash

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  gstreamer1.0-gl \
  gir1.2-gtk-3.0 \
  gir1.2-webkit2-4.1 \
  libwebkit2gtk-4.1-0

python3 -c "
import gi
gi.require_version('WebKit2', '4.1')
from gi.repository import WebKit2
print(f'WebKitGTK {WebKit2.get_major_version()}.{WebKit2.get_minor_version()}.{WebKit2.get_micro_version()}')
"
