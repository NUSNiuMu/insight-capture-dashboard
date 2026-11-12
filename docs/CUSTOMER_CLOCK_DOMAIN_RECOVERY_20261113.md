# Customer validation 17: clock service loss and timestamp-domain recovery

Date: 2026-11-13, Asia/Hong_Kong. Evidence:
`/home/niumu/workspaces/insight_capture_tests/customer-session-17/`.

## Reproduction

The head camera retained the verified shutdown-only library from session 16.
Its acquisition stayed running while its owned clock service was stopped for
about 16 seconds, then started. No new Take was created. A real Jetson IMU
subscriber observed 46,000 messages over approximately 115 seconds.

While the clock service was inactive, `is_time_sync` incorrectly remained `1`.
Starting the service temporarily changed it to `0`. The publisher then cleared
its time offset, emitting monotonic-since-boot headers, and re-established a
date-time offset when sync returned. This produced 800 wrong-epoch messages in
approximately two seconds, one backwards transition and a subsequent forward
jump of about 1.794 billion seconds. Acquisition PID 43931 never changed.
This is a publisher/input issue observed without a recorder, not MCAP corruption.

The phone remained online. The Jetson NTP monitor correctly reported failure
when the clock service was unavailable. Header epoch validation independently
detected the temporary wrong domain during recovery.

## Changes

Independent camera firmware commit `f34c8fcc892b66cfa66bc6c7920bfddb7cb3af14`:

- Retain an established per-acquisition timestamp offset during NTP loss and
  subsequent recovery. Log unavailable/restored synchronization without
  clearing or re-anchoring live headers.
- First synchronization of an independently started, never-synchronized
  camera retains its existing behavior. InsightCapture's idle startup gate
  requires synchronization before acquisition; do not claim arbitrary
  unsynchronized standalone startup has been fixed.
- Keep the session 16 shutdown repair. Pose-publish cadence code is unchanged.
  Header changes are comments only; no class-layout change, new undefined
  dynamic symbols or removed exports.

InsightCapture commit `c898c22238e26e0a91ae25d4c981c368674d3864`:

- Clear stale readiness before starting a new clock daemon.
- Add an idempotent `--clear-sync-state` command using the existing atomic flag
  publisher. Execute it with systemd `ExecStopPost`, covering service stop and
  abnormal exit. Cleanup does not start a daemon or set a clock.
- Eleven clock-flag and NTP-sampling checks passed, including daemon-launch
  failure preserving false readiness and repeated cleanup without rewriting.

These are independent owned changes: InsightCapture never reads the LooperHub
checkout at runtime/build/upgrade. No cross-project symlink, source directory,
configuration, state store or synchronization relationship was introduced.

## Real candidate verification

The head library and clock adapter were installed only while acquisition was
idle/stopped. The already-fixed shutdown exited normally. After startup, the
same real clock stop/start was repeated, followed by SIGKILL of the clock
service's main process. Its configured automatic restart recovered once.

| Measurement | Candidate result |
| --- | --- |
| Jetson raw IMU observation | 54,000 messages, 135.0005 s receipt span |
| Source span / rate | 134.9975 s / 400.0 Hz |
| Wrong epoch / backwards / duplicates | 0 / 0 / 0 |
| All 53,999 adjacent source intervals | Exactly 2.5 ms |
| Image-clock status samples | All 34 valid |
| Service-stopped flag samples | All `0` |
| Publisher during both clock faults | PID 44953 unchanged, no automatic restart |
| Clock service | One intentional crash restart, active at completion |
| NTP quality | Actually failed during both faults, recovered afterward |

The camera journal records two sync-loss/restoration pairs, explicitly retaining
the session offset. The phone screenshot shows `正在校时` during the outage,
then returns to the independent `定位尚未初始化` issue afterward. Preview remains
available over the direct hotspot path; ADB USB supplies only debugging access.

The periodic NTP monitor reports approximately every 10 seconds. The fixed
stop-phase first observations still had a previous good report; failure was
first observed about 7.6 seconds after the first stopped-service sample. This
is finite sampled visibility, not an exact outage-to-alert latency. Recording
start code makes a separate fresh measurement. This session did not exercise
NTP-only admission on the phone because head VIO was already unavailable.

Keeping the timestamp epoch does not assert that holdover meets multi-camera
accuracy. No NTP thresholds were relaxed, no data was dropped/rewritten, and
no clock-setting operation was added to running acquisition. Long drift,
packet-level NTP blackholes and actual recorded-MCAP verification of this
firmware change are still pending.

## Field identity and preservation

- Head library SHA-256:
  `d6dc55e65e0775a7ea3ecd4173323ff91afd7258edb72757c2e33846c117bcff`.
- Build ID: `59eeffd3a22c93b1da6a2d142504ea11b0664f6b`.
- Head clock script SHA-256:
  `74b013ee162314a12a9d41ddee7345d8a3259f0904359039b1934e3377be1149`.
- Head clock unit SHA-256:
  `f5da36242f360153c873fd8a7ef5e10d906b080adb52d4e731adbe5fabe522fd`.
- Runtime library mappings and file hashes checked; per-library field-fix
  provenance updated. Existing 2.1.3/old source-marker files remain unchanged
  and are not sufficient identifiers for the installed field patch.
- Jetson checkout is `c898c22`; the backend/phone implementation is unchanged
  by this deployment and the backend was not restarted.
- Thirteen head configuration/version hashes, including seven calibration
  files, match before/after. Protected formal/QA recording snapshots match.
- Formal task remains 22 Takes, next 23, idle; no recording created/deleted.
  Three image inputs, wrist VIO, necessary publishers and NTP are healthy.
  Head VIO remains `NOT_INITED`. Fallback storage is about 5.68 GiB free.
- All task-owned recovery timer units are gone; no lingering fault injection.

The two wrists were inspected read-only. Both map the exact original head
library SHA `7827cad98342c2d3c2983492e7580fa98f0540014820014d57045a79cbe4a907`
and identify as Insight3 NV12 variants, version 2.1.3. They have not received
this clock/firmware repair. Their currently initialized tracking processes were
preserved under the dark scene. Adequate illumination is still needed to
reliably validate each wrist's restart and this repair on both product variants.
Do not promote head verification to all three devices.

## Instrumentation limits retained in evidence

The original probe mistakenly passed unsupported `chronyc -t`; those diagnostic
outputs are invalid and excluded. Independent actual IMU timestamps, sync-file
reads, systemd state and Jetson clock measurements remain valid. The fixed probe
uses supported chronyc arguments and retains successful tracking/waitsync data.
The original 60-second recovery timer fired normally and disappeared before
cleanup; the harness misclassified its absence as an error. The camera service
was already restored. The fixed harness checks actual unit state before cleanup.

Main evidence: `baseline-result.json`, `imu-samples.json`, `clock-events.json`,
`fixed/imu-samples.json`, `fixed/clock-events.json`, `fixed-result.json`,
`validation-result.json`, `final-cameras.json`, `final-status.json`, before/final
data snapshots, phone screenshots and the rendered PDF.
This supersedes session 16's head-library/clock-domain status only. It does not
complete the broader acceptance ledger, merge main, push or build an installer.
