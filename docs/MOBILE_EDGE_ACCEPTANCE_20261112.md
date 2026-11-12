# Mobile console edge validation - 2026-11-12

This supplements `MOBILE_CONSOLE_ACCEPTANCE_20261112.md`. It records field tests
on the Jetson with one Insight9 and two Insight3 cameras. It is not a claim of
all-device, physical power-cycle, phone Safari, or indefinite endurance acceptance.

## Restrictions and their ownership

| Rule | Origin and assessment |
| --- | --- |
| Static camera IP whitelist, identity tied to IP/MAC | Removed in earlier implementation. Resolve current CDC-NCM USB peers and camera namespaces. Valid, non-conflicting IPv4 subnets may change without editing this project's configuration. |
| Reboot every camera OS on every Jetson boot | Removed in earlier implementation. Recover acquisition only when needed, once per Jetson boot. |
| Pause overview encoding during prepare/record/save | Explicit user requirement; retain. Closing the browser does not stop recording. |
| USB-only acquisition network; no AP forwarding; media follows page interface; no external STUN/TURN/CDN/NTP | Explicit offline/isolation requirement; retain. CDC-NCM is the currently supported discovery interface, not a promise that every network adapter protocol works. |
| NTP single-camera bound <= 10 ms; pairwise bound <= 20 ms; RTT <= 10 ms | Added engineering policy, not a hardware limit or a user-specified accuracy requirement. With every single-camera bound <= 10 ms, the 20 ms pairwise check adds no further rejection. With the current 5 ms system-correction allowance, the RTT check is also implied by the single-camera bound. Keep the measurements visible; revisit policy when a product accuracy target is agreed. This test run did not relax any threshold. |
| Actual camera system correction < 5 ms; five consecutive startup checks; 100 ppm runtime slew | Added engineering settings. Checking actual system time is necessary because an NTP server can advertise corrected time while its system clock is still converging. Five checks and 100 ppm are conservative choices, not immutable requirements. Startup correction is allowed only with that camera's acquisition stopped. |
| Boot recovery checkpoint before recording | Added to prevent capture starting while boot recovery can still restart acquisition. Retain while that recovery workflow exists; it is not an extra authentication permission. |
| Image source time versus receipt in [-2 s, +1 s], image sample < 2 s old; NTP result < 30 s old | Added wrong-epoch/staleness checks. They do not prove exposure synchronization. Freshness partly overlaps existing camera-health checks. |
| Three configured roles, fresh image/VIO, required topics, active task, writable disk with >= 5 GiB free | Existing capture preflight policy. Three-role collection needs these signals. Fixed 5 GiB can be conservative for short captures; a duration/capacity policy would be more useful than treating it as a safety law. |
| Primary disk, Internet UTC, successful mapping/calibration before raw recording | Not mandatory in the current mobile raw-capture path. The absent primary disk produces a fallback warning. Clock alignment and spatial calibration remain separate. |
| No task mutation during recording; duplicate commands; stale stop protection | Existing intent, retained. This run fixed the previously unguarded preparation interval and malformed command IDs. |
| VIO nominal 97/99 Hz and 0.5% estimated-loss cutoff | Existing post-record quality policy, not a new start gate. A failed result remains visible; do not lower the cutoff to obtain a passing report. |

## Defects reproduced and fixed

1. Task activation could race an already accepted start while preflight was
   running. A shared recording transition guard now covers prepare/start/save;
   task handlers check it after reading request bodies.
2. A closed signaling socket left preview permanently disconnected. Visible,
   online, idle preview retries after three seconds, and clears its own error
   when decoded video resumes. Hidden or recording pages cancel retries.
3. Same-day frontend builds reused the same immutable resource URL. URLs now
   include the SHA-256 prefix of the actual JS/CSS contents.
4. A failed history detail lookup could leave the preceding Take visible. Clear
   the previous detail while switching records.
5. Non-string command IDs were coerced into valid strings. Reject null, booleans,
   numbers, objects, arrays, blank strings, and IDs longer than 100 characters.
6. The device page showed the start-time clock snapshot throughout recording.
   It now reads current NTP and image-clock measurements without running the
   full start preflight again or stopping capture.

## Real IP reuse test

Move the head camera from `169.254.40.1 / .40.2` to `.60.1 / .60.2`, freeing the
old head subnet. Move the left wrist from `.20.1 / .20.2` to `.50.1 / .50.2`,
then to `.40.1 / .40.2`. The `.50 -> .40` operation reached complete preflight
readiness in 44.50 s. It remained `insight3_b` (left wrist), while the head stayed
`insight9_a` at `.60.1`. Preview, topics, VIO and fresh NTP measurements recovered
without changing project configuration. Camera boot IDs and chronyd PIDs did
not change during the address operations.

This tests reuse of another role's old subnet, not just an unused address.
Simultaneously duplicated/conflicting subnets are not supported configurations.

With the reused addresses still active, a Jetson software reboot restored USB
SSH in 52.56 s, the API in 58.93 s, camera inputs in 108.87 s, and full recording
readiness plus all four services in 115.79 s. The role mapping remained correct.
Timing includes polling; this is one warm reboot, not a cold-boot distribution.
After testing, left was restored to `.20.1 / .20.2` (40.91 s to ready), then head
to `.40.1 / .40.2` (51.20 s). Right remained `.30.1 / .30.2` throughout.

## Validation matrix

- 153 automated checks passed, including 32 new edge cases. Coverage includes
  task/start concurrency, missing image/VIO/topic, bad/stale clock samples,
  image epoch limits, low/read-only storage simulation and malformed commands.
  Storage exhaustion was simulated; the recording disk was not deliberately filled.
- 15 real Chromium browser cases passed with no captured JavaScript errors:
  overview and all single sources, signaling recovery, expand/Escape, rapid
  switching, two viewers, client offline/reconnect, history isolation, language
  persistence, and 320/390/768/932 px viewport layouts.
- A real 123.260 s QA recording with changed camera addresses retained one Take
  through duplicate start/stop requests, conflicting start, stale stop,
  client offline, browser close/rejoin, and a discovery-service restart.
  Preview worker and go2rtc were absent during recording; preview resumed after save.
- A separate intentional-fault Take stopped and restarted right-camera
  acquisition. The other cameras stayed ready, recording continued in the same
  Take, the failed input recovered, and the Take saved. The current clock age
  changed from 7.28 s to 2.18 s during recording, confirming live status refresh.
  This is a software acquisition fault, not a physical USB unplug test.
- Formal task `egohalo_4x4_static_20261029` remains 22 Takes, next Take 23. QA data
  is under `qa_edges_20261112`; the fault Take must not be counted as clean data.

## Remaining quality finding

The clean QA Take has 231,224 messages. All five image streams and three 400 Hz
IMU streams had no estimated missing samples under the existing timestamp
audit. Head VIO averaged 98.42 Hz against the existing nominal 99 Hz: 71
estimated missing samples (0.58%), exceeding the existing 0.5% cutoff. Its
largest timestamp gap was 28 ms. The MCAP quality result is therefore FAIL.

USB receive error/drop and UDP receive-error counter increments were zero;
that does not prove that every source publish reached storage. The current
LooperHub source only publishes advancing VIO source timestamps and skips
overdue scheduling slots, but that checkout is not evidence of the exact
camera binary. A source-side versus recorder-side timestamp comparison is
still needed to distinguish source cadence from transport/recording loss.

NTP pairwise conservative bounds were 12.4512 ms at start and 12.0849 ms at stop.
These pass the current clock policy; they do not establish hardware exposure
synchronization or spatial calibration accuracy.

## Evidence and acceptance scope

Local evidence: `/home/niumu/workspaces/insight_capture_tests/20261112-edge/`.
Important files: `ip-left50-to40.json`, `browser-edges.json`,
`recording-edges.json`, `camera-fault.json`, `long-recording_manifest.json`,
`long-recording_network_audit.json`, `mcap-long.txt`, and `reboot.json`.

Physical power removal, real phone AP/Safari behavior, long-duration thermal
and drift endurance, physical replacement by another camera serial number,
and complete public-map geometric calibration are not accepted by these tests.
The independent source/assets/build contract is unchanged. No Ego service,
configuration, shared source path, symlink or synchronization relationship was added.
