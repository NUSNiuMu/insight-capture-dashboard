# Customer field review 13: camera shutdown and dark startup

Date: 2026-11-13, Asia/Hong_Kong. Evidence directory:
`/home/niumu/workspaces/insight_capture_tests/customer-session-13/`.

## Outcome and current device state

The head camera's installed application aborts during an ordinary idle service
stop. A narrowly patched library stopped successfully in the corresponding
field test. Its VIO scheduling change remains unaccepted because neither the
candidate nor the restored original initialized VIO under the current very dark
camera input. The original library was restored atomically and verified by hash.
The camera service and image streams are running; full capture preflight is
**not ready** because head VIO is missing. Do not report this as a completed
firmware deployment or a solved VIO frequency issue.

InsightCapture runtime remains `08f8a90`. Its code, build, upgrade, resources and
state do not acquire any LooperHub or ego_dashboard dependency. LooperHub work is
a separate camera-firmware investigation and independent worktree. No main
merge, push or customer installation package was produced.

## Reproducible camera baseline

- Installed source marker: `0d087d9a7fbbbfc6b2478d6c3f254153efb91c60`, version 2.1.3.
- The marker alone does not build: three obsolete `pipes_` references and a
  duplicate exposure-log definition require the existing historical fixes from
  `19da53163331ea9052dc2285001e7d93d85583b5`.
- Reproduction commit `d73f7ad` in the independent worktree
  `/home/niumu/workspace/.worktrees/looperhub-insight-vio-20261113` contains those
  build corrections. After the original CMake install RPATH transformation, the
  library is byte-for-byte identical to the device library.
- Original SHA-256:
  `7827cad98342c2d3c2983492e7580fa98f0540014820014d57045a79cbe4a907`.
  Build ID: `534b13996703567e65538b7718e2839a3c216e12`.
- Candidate commit `66fe4bd`, SHA-256:
  `d622ec76e5f1fd59f425fb99d3bb527d2e7fbed261450dde2d7f99d875c8164e`.
  Build ID: `ccf2787b53521f4e5153e8c0e1c1cd12ab9a8cd7`.
- Same old toolchain and dependency contents; no public class-header changes,
  new undefined dynamic symbols or removed exported symbols. ARM64 library
  build succeeded. The original main LooperHub checkout remained clean.

Loading caveat: an isolated `RTLD_NOW` load fails for both original and candidate
on the same pre-existing `avformat_close_input` reference; `RTLD_LAZY` succeeds
for both. This was recorded, not treated as a new candidate dependency. It does
not prove every dormant codec path is usable and deserves a release-link audit.

## Confirmed shutdown defect

The original starts `fps_switch_thread_` but never stops or joins it in the node
destructor. The worker can remain in its condition wait after ROS shutdown.
Destroying its joinable `std::thread` calls `std::terminate`.

The actual installed destructor's terminating branch reads member offset
`0x17b8`; a cross-compiled layout probe against the reproduced source identifies
that exact offset as the FPS thread. Historical crash traces and this run's new
trace both contain `HobotFullNodeD2Ev+0x1924`.

The candidate clears the running flag and pending request under the existing
mutex, broadcasts the condition, and joins the worker before tearing down the
pipelines it can reconfigure. The other candidate change uses steady-clock
deadlines for 100 Hz pose publishing, skips overdue slots, and rejects repeated
raw monotonic pose timestamps before applying the wall-clock offset.

| Operation | Original | Candidate |
| --- | --- | --- |
| Stop from idle ROS mode | `Result=signal`, `SIGABRT (6)` | `Result=success`, inactive, MainPID 0 |
| Newly created crash files | 1 | 0 |
| Stop command plus evidence collection | 3.9674 s | 4.6026 s |

These times include instrumentation and are not pure shutdown latency. The
candidate stop was tested while VIO was uninitialized and no FPS switch was
pending; stop during actual FPS reconfiguration remains untested.

Evidence: `original-stop.json`, `candidate-clean-stop.json`,
`installed-destructor-disassembly.txt`, `abi-layout.json`, and build manifests.

## Startup control and raw input

The active service runs `/etc/init.d/looper/all_run.sh`, which differs from the
unused `/app/all_run.sh`. Its real mode query is under `setting/`; ROS mode 1 and
the existing boot marker were checked before stopping. Neither network mode nor
USB configuration was switched.

First observed image recovery from the start of stop/replace/start operations:
22.98 s for the candidate, 21.13 s for the restored original. These include the
existing NTP startup synchronization and are not Jetson reboot measurements.
Images returned at about 30 FPS. Candidate observation lasted about 190 s and
original observation about 120 s; both reported `NOT_INITED` with no valid VIO.

Head left and right rectified mono8 inputs were sampled directly, three frames
per eye, 544×640. Mean values were 2.73–3.70/255; the 95th percentile was at most
7/255; 82.6–94.0% of pixels were at most 5/255. The candidate log showed camera
and VIO manager construction succeeded and NTP was synchronized. This supports
testing with adequate lighting and scene texture next; it does not establish
lighting as the sole cause or exclude exposure/sensor/initialization defects.

The Android page stayed connected, displayed the missing input and disabled
start recording. Screenshots are from the real OnePlus via USB. The page and
media continued through the Jetson AP, not an ADB reverse data path.

## IMU observation cross-check

A short combined image-statistics probe observed about 386 Hz; its callback
work and small sensor queue make that insufficient evidence of source loss.
A separate 90-second paired subscription used the publisher's BEST_EFFORT
reliability and a depth of 2000, with no image processing or MCAP creation.

Both probes identified the same publisher GID. The common source window was
85.0 seconds:

| Measurement | Camera-side Python observer | Jetson observer |
| --- | ---: | ---: |
| Messages | 22,854 | 34,001 |
| Source-time rate | 268.8588 Hz | 400.0000 Hz |
| Duplicate/backward stamps | 0 / 0 | 0 / 0 |
| Maximum gap | 40 ms | 2.5 ms |
| Unique to this observer | 0 | 11,147 |

Every camera-observer timestamp and numerical payload exists in the Jetson
stream. The camera observer accumulated up to about 5.04 s of backlog; its
sampling bottleneck is not further localized. It cannot represent the complete
published stream. Jetson received an uninterrupted 400 Hz source-time window.
This is not a VIO or recorded-MCAP acceptance claim.

Evidence: `baseline-head-inputs.json`, `camera-imu-samples.json`,
`jetson-imu-samples.json`, `paired-imu-result.json`.

## Preservation and remaining acceptance

- Original library restored, hash verified; final head PID 40288, service active
  with no automatic restarts at the check. The candidate is not left installed.
- All 13 checked configuration, calibration and version files match their
  before hashes, including seven files below `/app/calibration`.
- No recordings were created, deleted or changed. Formal task remains 22 Takes,
  next Take 23. Protected formal Take 22 and QA03 Take 1 metadata hashes and MCAP
  sizes/mtimes match. Existing recording data was not removed to free space.
- Three-camera NTP check passes; head VIO readiness does not. Main recording disk
  is still absent; fallback free space is approximately 5.68 GiB.
- Pending: illuminated VIO initialization and rate comparison; real MCAP
  quality; exit during FPS switching; actual iPhone; completed mobile download
  verification; full spatial calibration; long-duration recording and thermal
  behavior; physical power-cycle (still deferred by the user).
- Further customer-facing issue to examine: the failure page only says waiting
  for VIO, without conveying the observed initialization state; expanded failure
  content also pushes part of the overview below the first-screen action bar.
  This run documents the behavior; it does not claim those UI details are fixed.

Evidence correction: Session 12's `legacy-pose-source-reference.cpp` was
accidentally extracted from `_publish.cpp`. Session 13 contains the correct
`_vio.cpp` excerpt without overwriting the older raw evidence. The actual
installed-binary disassembly supporting the relative-sleep conclusion remains
valid.

Report: `InsightCapture_相机退出崩溃修复与暗光启动对照_2026-11-13.pdf`, 4 pages,
SHA-256 `35fc2da3ae90929215c96d2bc8060d72057bae72eade41dd2b046c9ac9af3980`.
All four pages rendered and visually reviewed.
