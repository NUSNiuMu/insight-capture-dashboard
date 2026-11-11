#!/bin/sh
# A forced SSH command: query the caller's USB clock without setting time.
set -eu
set -- ${SSH_CONNECTION:?Missing SSH connection}
peer=$1
case "$peer" in *[!0-9.]*|'') exit 1;; esac
exec /usr/sbin/ntpdate -q -p 3 -t 1 "$peer"
