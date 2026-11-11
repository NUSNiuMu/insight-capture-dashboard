#!/usr/bin/env bash
set -euo pipefail
iptables -w -N INSIGHT_CAPTURE 2>/dev/null || true
iptables -w -F INSIGHT_CAPTURE
iptables -w -A INSIGHT_CAPTURE -i capture_ap -j DROP
iptables -w -A INSIGHT_CAPTURE -o capture_ap -j DROP
iptables -w -C FORWARD -j INSIGHT_CAPTURE 2>/dev/null || iptables -w -I FORWARD 1 -j INSIGHT_CAPTURE
