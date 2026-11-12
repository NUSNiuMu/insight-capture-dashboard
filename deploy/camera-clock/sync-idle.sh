#!/bin/bash
# Only a stopped acquisition process may step time. All sensor starts serialize
# here, so running recordings can never reach the clock-setting commands below.
set -euo pipefail
exec 9>/run/insight-capture-clock/sync.lock
flock 9
root=/opt/insight-capture-clock
if pgrep -x insight_full >/dev/null || pgrep -f '^/userdata/install/lib/insight_full_uvc/insight_full_uvc([[:space:]]|$)' >/dev/null; then
    exec "$root/wait-sync.sh"
fi
clock() { "$root/usr/bin/chronyc" -h /run/insight-capture-clock/chronyd.sock "$@"; }
# A rebooted Jetson can have a new RTC epoch. Old regression samples must not
# make waitsync pass before fresh measurements of that clock have arrived.
for connection in $(seq 1 10); do
    if clock reset sources; then break; fi
    sleep 1
done
clock burst 4/8
for attempt in $(seq 1 100); do
    if clock waitsync 1 0 0 0.1; then
        clock makestep
        clock burst 4/4
        stable=0
        for sample in $(seq 1 5); do
            sleep 1
            if clock waitsync 1 0.005 0 0.1; then
                stable=$((stable + 1))
            else
                break
            fi
        done
        if [[ $stable == 5 ]]; then exit 0; fi
    fi
    sleep 1
done
exit 1
