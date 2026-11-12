# Customer session 12: live QC visibility and head VIO source audit

Date: 2026-11-13 (Asia/Hong_Kong).
Field branch: `feat/insight-mobile-preview-20261111`.
Deployed source: `08f8a908178d8bc89331a7769753255a38648f79`.
Backend warning projection was introduced in `6501988`; `08f8a90` updates
operator-facing device names and localization wording only.

## Live recording warnings

Reproduced with the real `ActiveQcMonitor`, a temporary `SessionTakeStore`,
and `MobileCaptureRoutes`: sustained low storage persisted a `storage_low`
event, but `/api/mobile/status` omitted it. Recording-time UI also hides
admission issues. Thus an operator could miss a warning until reviewing the Take.

The mobile snapshot now projects unresolved timeline events into `live_alerts`.
It requires a recording/finalizing Take, matching `bag_path`/`output_path`,
and a recording/saving lifecycle. Resolved events disappear from the live
projection without deleting history. Starting, idle, playback and another Take
do not inherit old warnings. The full timeline is not sent on every poll.

The UI shows current messages and a fixed, at-least-44-pixel alert link across
capture, history and device pages. The stop button remains available. A detached
page labels cached warnings as the last received device state. Labels use camera
roles; localization errors are not reinterpreted as proof of a particular cause.

Admission, quality and existing QC sustain/free-space thresholds are unchanged.
No automatic recording stop or new capture gate was added.

Validation:

- Before-fix reproduction reached a persisted `storage_low` event, then failed
  because `live_alerts` was absent. No actual disk was filled.
- 102 relevant backend tests passed, including 12 new warning cases; 10 project
  structure checks and 33 frontend tests passed; frontend build passed.
- Desktop Chrome: 18 combinations of Chinese/English, three viewport sizes
  (384x703, 320x568, 832x255), and capture/history/device routes. Checked stop
  reachability after scrolling, alert navigation/focus, resolution, saving,
  starting, idle and camera-role wording. No POSTs or page errors.
- Fault input and recording-state UI were simulated. This is not a live phone
  acceptance of disk exhaustion, I/O failure or authoritative recording loss.

## Real phone and deployment

USB ADB accesses the OnePlus 7T Pro (HD1910), Android 11, Chrome 132. The actual
page is `http://10.77.0.1:8765/mobile#capture`; media uses the phone's hotspot
address `10.77.0.106` and Jetson `10.77.0.1:8768`. USB supplies debugging access.

Idle backend restart to first observed preflight readiness: 4.5468 seconds.
This is not a Jetson boot measurement. The old phone page automatically
reconnected; native Chrome refresh loaded the new assets. Device-page navigation
released the peer, and return to capture decoded 1280x1280 overview video.
The subsequent wording-only update left backend PID 1383571 unchanged.
Final JS SHA-256:
`c3bbf3f95edfcb6895ce8405f9f3bfed31cf4e5d88b3c34d0aa119b7d87545cc`.

One uninterrupted 81.8931-second phone observation after backend deployment:
2,457 new decoded frames, reported 30 FPS, no increase in RTP loss or freezes;
presentation dropped 6 of 2,456 frames. Do not describe this as zero frame loss.

No recordings were created/deleted. The formal task remains 22 Takes / next 23.
Protected formal Take 22 and QA03 Take 1 metadata hashes and MCAP sizes/mtimes
match the previous session. The device finishes idle and preflight ready, with
about 5.6824 GiB free on fallback recording storage. Phone is back on Chinese
portrait overview. No main merge, push or customer installer build was done.

## Head VIO: source phenomenon confirmed, firmware repair pending

Concurrent camera-local and Jetson subscribers observed the same publisher GID.
The aligned 199.3275-second window contains 19,603 messages on each side at
98.3406705 Hz. Ordered semantic payloads (header and pose values) are identical;
camera-only and Jetson-only counts are zero. Both see 19 duplicate timestamps,
zero backward timestamps, and a maximum 27.5 ms gap.

Preview on/off/on phases, discarding five seconds at each boundary:

| Phase | Effective span | Source rate | Duplicate stamps | Ordered payloads |
| --- | ---: | ---: | ---: | --- |
| On 1 | 53.8075 s | 98.3134321 Hz | 4 | Identical |
| Off | 56.9875 s | 98.3549024 Hz | 5 | Identical |
| On 2 | 50.1100 s | 98.4234684 Hz | 4 | Identical |

Turning preview off did not restore the existing 99 Hz QC reference. These short
windows do not prove no effect at every load or replace recording endurance.
The previous MCAP/source comparison is documented in
`CUSTOMER_VIO_AUDIT_20261112.md`; this session made no new MCAP recording.

Read-only inspection of the library mapped by the running head process confirms
legacy `HobotFullNode::posePublishThread`, with a fresh loop start each iteration
and `nanosleep(10ms - work time)`. Wake-up/scheduling delay accumulates in this
relative cadence, a plausible contributor not yet quantitatively isolated.
The actual library SHA-256 is
`7827cad98342c2d3c2983492e7580fa98f0540014820014d57045a79cbe4a907`;
build ID `534b13996703567e65538b7718e2839a3c216e12`.
Disassembly sites: 0x10cb7c loop clock, 0x10cc90 elapsed subtraction,
0x10ccb0 remaining duration, 0x10ccfc nanosleep.

Newer LooperHub code has absolute-deadline scheduling and timestamp advance
deduplication, but its module/class layout differs from the installed library.
It was inspected only as a diagnostic reference: no camera firmware write,
source sharing or runtime dependency was introduced. Exact installed firmware
Git provenance and a compatible minimal firmware fix remain unresolved.

During the probe, system-wide average CPU busy was 41.66% camera / 65.84% Jetson;
maximum read sensor temperatures were 60.42 C / 75.13 C. These include unrelated
services and are not recording thermal/endurance certification.

## Evidence and remaining acceptance

External QA evidence directory (not a runtime/build dependency):
`/home/niumu/workspaces/insight_capture_tests/customer-session-12/`.
Contains paired raw samples, disassembly, before-fix reproduction, browser
results, real phone screenshots/statistics, deployment and protected-data checks.
The three-page PDF was rendered and every page visually inspected.

Pending: actual iPhone, download completion/file verification, physical power
removal, complete spatial calibration, long recording endurance, and head VIO
frequency. Software reboot or page success must not imply those are accepted.
