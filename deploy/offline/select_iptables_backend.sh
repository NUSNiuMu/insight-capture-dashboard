#!/usr/bin/env bash
set -euo pipefail

# Probe in a disposable namespace; never add test rules to the host.
works() {
    unshare --net -- sh -c '
        "$1" -w 5 -t filter -A OUTPUT -j ACCEPT &&
        "$1" -w 5 -t nat -A POSTROUTING -j MASQUERADE
    ' sh "$1" >/dev/null 2>&1
}

current=$(command -v iptables || true)
[[ -n "$current" ]] || exit 0
version=$("$current" --version 2>/dev/null || true)
[[ "$version" == *nf_tables* ]] || exit 0
works "$current" && exit 0

legacy=$(command -v iptables-legacy || true)
if [[ -z "$legacy" ]] || ! works "$legacy"; then
    echo 'The kernel cannot run the required iptables filter/NAT rules.' >&2
    exit 1
fi
update-alternatives --set iptables "$legacy" >/dev/null
ipv6=$(command -v ip6tables-legacy || true)
if [[ -n "$ipv6" ]] && update-alternatives --query ip6tables >/dev/null 2>&1; then
    update-alternatives --set ip6tables "$ipv6" >/dev/null
fi
printf '%s\n' legacy
