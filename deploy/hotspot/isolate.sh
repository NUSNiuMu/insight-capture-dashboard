#!/usr/bin/env bash
set -euo pipefail
iptables -w -N INSIGHT_CAPTURE 2>/dev/null || true
iptables -w -F INSIGHT_CAPTURE
iptables -w -A INSIGHT_CAPTURE -i capture_ap -j DROP
iptables -w -A INSIGHT_CAPTURE -o capture_ap -j DROP
iptables -w -C FORWARD -j INSIGHT_CAPTURE 2>/dev/null || iptables -w -I FORWARD 1 -j INSIGHT_CAPTURE

# Standard HTTP entry for phones on the capture hotspot. Keep requests direct
# to the backend so its local address and WebRTC interface selection stay intact.
iptables -w -t nat -N INSIGHT_CAPTURE_HTTP 2>/dev/null || true
iptables -w -t nat -F INSIGHT_CAPTURE_HTTP
iptables -w -t nat -A INSIGHT_CAPTURE_HTTP -i capture_ap -p tcp --dport 80 -j REDIRECT --to-ports 8765
iptables -w -t nat -C PREROUTING -j INSIGHT_CAPTURE_HTTP 2>/dev/null || iptables -w -t nat -I PREROUTING 1 -j INSIGHT_CAPTURE_HTTP
