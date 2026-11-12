# Customer validation 18: actual NTP packet loss with acquisition running

Date: 2026-11-13, Asia/Hong_Kong. Evidence:
`/home/niumu/workspaces/insight_capture_tests/customer-session-18/`.

This follows the clock-service stop/recovery and crash tests in
`CUSTOMER_CLOCK_DOMAIN_RECOVERY_20261113.md`. It tests actual network loss
without stopping the head acquisition or clock process. No application or
firmware code changed, and no Take was created.

## Controlled fault and recovery

The Jetson dropped only UDP packets with source or destination port 123 on
the head USB link, `169.254.40.1` to `169.254.40.2`, interface
`enx020000002801`. INPUT and OUTPUT jumps matched the exact interface and
endpoint pair. A task-owned chain `IC_QA18_NTP` supplied the drops; unrelated
ports and network interfaces were unaffected.

A separate systemd recovery timer was armed for 150 seconds before applying
the fault. Manual recovery completed first. The helper removed only its
own jumps and chain. The complete `iptables -S` output matched the initial
rules afterward, the chain was absent, and no `insight-qa18-*` units remained.
The independent PC-to-Jetson USB debugging link remained available.

## Observed results

| Measurement | Result |
| --- | --- |
| Actual blocking window | 125.342 s |
| Firewall NTP packet drop count | 54 |
| Head IMU messages received on Jetson | 80,000 |
| Source timestamp span / rate | 199.9975 s / 400.0 Hz |
| Wrong epoch / backwards / duplicate timestamps | 0 / 0 / 0 |
| All 79,999 adjacent source timestamp intervals | Exactly 2.5 ms |
| Head acquisition / clock processes | Both unchanged throughout |
| Sampled input image freshness / head header clock validity | All valid |
| Phone comparison window | 182.648 s, same media SSRC |
| Phone additional decoded frames | 5,479, approximately 29.998 fps |
| Additional RTP lost packets / freezes | 0 / 0 |
| Additional video presentation drops | 13 |

The head chrony source reach register decayed from 377 to 0 and selection
changed from `*` to `?`; the last good sample age grew to about 120 seconds.
The camera readiness file stayed `1` during this short holdover. Nevertheless,
the Jetson's actual NTP round-trip measurement correctly reported failure.
After packets were restored, the source was selected again and the measurement
passed. This demonstrates why the flag alone cannot establish current NTP
reachability or synchronization quality.

The first sampled NTP failure was approximately 5.9 seconds after blocking;
the first sampled recovery was approximately 11.5 seconds after unblocking.
These are observations from an approximately five-second sampling loop, not
precise detection or recovery latency bounds. The phone showed the clock
warning and kept playing its existing preview without a refresh.

## Preservation and scope

Head acquisition PID 44953 and clock PID 45329 were unchanged. Clock restart
count 1 belongs to the intentional crash in session 17; no new restart occurred.
The head retains session 17's library and owned clock adapter. The wrists were
not restarted or updated.

The protected recording snapshot matches session 17, including formal Take 22
and the selected QA recording's metadata hashes and MCAP size/mtime. This is
not a claim that every recording file was fully hashed. The formal task remains
idle with 22 Takes and next Take 23. Final NTP checks, three fresh image inputs
and required topic publisher checks pass. Head VIO remains `NOT_INITED`;
fallback storage remains approximately 5.68 GiB free.

This finite test validates IMU timestamp continuity under actual NTP loss on
the repaired head camera. It does not prove holdover absolute accuracy, all
topic timestamps, either wrist variant, actual recorded MCAP under this fault,
long drift, or an otherwise-ready phone's NTP-only recording admission. Those
acceptance items remain open. No thresholds were relaxed, no recordings were
created or deleted, and no customer installer, merge or push was performed.

Primary evidence: `events.json`, `firewall-start.json`, `firewall-middle.json`,
`firewall-restored.json`, `imu-samples.json`, `validation-result.json`,
before/after camera identities and status, phone screenshots/statistics,
`final-status.json` and `final-data.json`. The existing clock validation PDF
now includes this test as page 4; all four rendered pages were visually checked.
