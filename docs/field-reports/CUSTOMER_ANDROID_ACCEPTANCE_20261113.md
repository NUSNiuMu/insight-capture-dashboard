# Real Android phone / offline hotspot validation

2026-11-12 late evening to 2026-11-13, Asia/Hong_Kong. OnePlus 7T Pro HD1910,
Android 11, Chrome 132. Phone controlled through the computer's existing ADB
server on port 5039; checking only the default 5037 initially missed it.
These are real phone observations, separate from previous desktop Chromium tests.

## Changes deployed

- `73aa96f`: distinguish wrong source-clock epochs from sample-loss estimates;
  bracket NTP wall/monotonic samples without including delayed socket close;
  show recovered readable data and individual downloads on failed Take details.
- `c364307`: bounded-memory recovered ZIP with one Take-named directory and
  original MCAP, metadata and recovery manifest; correct the reserved Take label
  during preparation. 218 backend/system and 25 frontend tests plus build passed.
- `5a1fb79`: keep the active AP channel when the office station disconnects.
  Only first AP activation uses the configured fallback channel. Existing STA
  still determines the shared-radio channel. Shell parsing and a real phone
  offline transition passed. The deployed script SHA-256 is
  `c4f1b4c94bf8a73d1924a961f63954e0363b6e3d618769083cd9dea2d4e4122f`.

The hotspot change was installed into the project's checkout and its installed
guardian; the backend was not restarted for that change. Runtime code is on the
field branch, not merged main or a rebuilt customer installer. No Ego directory,
service, settings, shared source, symlink or sync relationship was introduced.

## Phone evidence

- Overview 1280x1280; head 1088x1920; wrist-left views 544x640. Steady samples
  around 29-30 fps. Expand, browser fullscreen and landscape worked. Original
  Android rotation settings were restored after the orientation test.
- A selected ICE pair was phone `10.77.0.106:40086` to Jetson `10.77.0.1:8768`
  over UDP. AP interface monitoring independently observed media and HTTP.
- Screen-off released preview; wake to first decoded image was approximately
  1.076 s. Wi-Fi disabled for eight seconds then enabled recovered in 5.558 s.
- Preparing, recording and saving released preview; save resumed it. Two Takes
  have start and stop POSTs from the actual AP client. History browsing did not
  change the active capture task.
- Idle backend deployment restart recovered the existing phone page without a
  manual refresh. Frontend cache version was separately refreshed/verified.
- One stream later accumulated 28 RTP lost packets and presentation-frame drops;
  isolated clean samples must not be described as an all-session zero-loss test.

## Offline defect and retest

Before the fix, losing STA changed an active AP from channel 157 to default 36.
Android automatically joined its saved office Wi-Fi. After the fix, retesting
with STA/AP on channel 52 kept the phone on the AP, on the same RTP stream:
decoded frames 524 -> 971, cumulative packet loss unchanged at 28, freezes zero.

The second offline window was Jetson epoch 1794500523.7048 through default-route
restoration at 1794500703.8897. No IPv4/IPv6 default routes remained; an external
route lookup failed. Take 3 began and ended in this window through the phone UI.
Office Wi-Fi successfully reconnected afterward and the USB route was restored.

## Recorded files, not only status messages

| QA phone Take | Actual MCAP span | Messages | Empty messages | Head VIO |
| --- | --- | --- | --- | --- |
| 1 | 6.702300096 s | 12,577 | 0 | 98.17 Hz; estimated deficit 0.75% |
| 2 | 12.573713303 s | 23,607 | 0 | 98.43 Hz; estimated deficit 0.56% |
| 3 | 3.903936988 s | 7,328 | 0 | 98.01 Hz; estimated deficit 0.78% |

Take 2 started from the phone but was stopped by the QA-only 12-second USB guard;
it does not prove the phone stop path. Take 3 filled that acceptance gap. The
guard was a test process, not an application recording-duration restriction.

All audited source timestamps had zero wrong-epoch diagnostics. All three deep
checks still failed on the head VIO topic under the existing 99 Hz / 0.5% policy.
Estimated missing samples are not proof of network loss. Wrist public-map
localization remained incomplete, so quick QC correctly marks the Takes suspect.
No thresholds were changed to obtain a pass.

Take 3 offline NTP: right wrist -0.0899 ms, left wrist +0.0459 ms, head +0.1254 ms;
conservative pairwise bound 12.4881 ms under the existing 20 ms rule. This does not
establish exposure synchronization, external UTC accuracy or spatial calibration.

## Unaccepted or unresolved

- Chrome displayed “无法安全地下载文件” for the recovered ZIP on the HTTP origin.
  User confirmation was requested and left pending. No browser security warning
  was bypassed. No final phone ZIP / CRC verification may be claimed yet.
- An old unconfirmed-command message remained after reconnect while the current
  state was saved; a new successful command cleared it. Reconciliation across
  overwritten command status / backend restart still needs improvement.
- The capture page's first screen leaves much of the preview below other status
  content; landscape toolbar space is large. Expanded preview works.
- True NTP loss can still make the camera firmware change source-clock domain.
- iPhone/Safari, physical camera replacement/unplug, cold power removal,
  calibration geometry, long thermal/drift endurance and real disk exhaustion
  are not covered by these short Android recordings.

Previous `.50.1/.50.2 -> .40.1/.40.2` address reuse and software reboot evidence
remains in [`MOBILE_EDGE_ACCEPTANCE_20261112.md`](MOBILE_EDGE_ACCEPTANCE_20261112.md); this phone session did not repeat
those network-address changes.

## Preserved state and evidence

The formal task is restored to 22 Takes, next 23, not recording. The QA phone task
retains three Takes. Protected formal Take 22 / QA03 Take 1 metadata hashes and
MCAP size/mtime match the pre-test snapshots. No recording was deleted. The
original fallback NVMe has about 5.82 GiB free; the missing primary disk warning
remains visible. QA monitors and duration guard ended.

Local evidence directory:
`/home/niumu/workspaces/insight_capture_tests/customer-phone-acceptance/`.
Relevant files: `phone-network.jsonl`, phone `*-stats`/view JSON and screenshots,
`jetson-offline-fixed.jsonl`, `offline-fixed-record-cycle.json`, `take*-direct-mcap.json`,
`take*-integrity.json`, `formal-data-before.json`, `formal-data-after.json`,
`deploy-hotspot.log`, and the rendered six-page PDF report.

Clock-classification/NTP experiment evidence is in sibling `customer-session-09/`.
The delayed-close comparison used three cameras x three measurements: old code
misclassified all nine with a 25 ms injected close delay; corrected code accepted
all nine, with offsets -0.163 to +0.4442 ms. This isolates the sampling bug rather
than proving that a real wall-clock step can never occur.
