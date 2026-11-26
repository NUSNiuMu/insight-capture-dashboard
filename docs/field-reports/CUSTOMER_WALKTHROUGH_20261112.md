# Customer workflow validation - 2026-11-12, session 01

Baseline: `91417fe`, Jetson and all three physical cameras, accessed through the verified
USB debug link. This session operated the rendered phone UI and used browser request
failure injection. It did not use a real iPhone or Android phone. Both are required targets.

## Reproduced and corrected

| Customer action | Observed failure | Verified resulting behavior |
|---|---|---|
| Start a normal capture while preview is playing | Intentional preview shutdown produces a persistent WebRTC error | Pending start immediately pauses the UI; normal preparation/recording/save has no false preview error |
| Open history during recording, then stop from history | Saved Take remains “recording / 00:00” until navigation | History follows state changes and refreshes while visible; final duration appears automatically |
| Inspect a saved Take with unlocalized wrists | Green “saved” obscures quality; detail says only `suspect` / generic anomaly | List shows review requirement; detail names each unlocalized wrist and distinguishes storage from quality |
| End or change the current task | Previous save card and Take count remain visible | Save feedback is tied to the current Take and output; no-task state explicitly asks for a task |
| Two pages start or stop together | Rejected duplicate leaves one page with a persistent conflict despite successful capture | Both pages converge on the same Take; stop reconciliation must match the output path |
| Switch task successfully, then fail the task-list GET | Successful mutation is reported as failure and dialog stays open | Acknowledged mutation stays successful; later read failure is handled separately |
| Disconnect while returning from Take detail, then reconnect | Cached records disappear; error remains after reconnection | Same-task records remain readable; read warnings clear on successful refresh |
| Switch to English and reload | Language/diagnostic/storage/clock headings remain Chinese | Added missing translations; language persists across reload |

The frontend also discards late status/task responses so they cannot overwrite newer
state. These changes introduce no additional recording gate and do not relax clock or
data-quality criteria. Task creation, mutation failures and read failures have separate
feedback. Actual failed commands remain visible; read recovery does not erase them.

## Evidence and checks

Own task: `task_7028c2ad2df1ab3002b4963f0d3af550`, five saved Takes, durations
6.93 / 35.00 / 25.07 / 31.05 / 23.45 seconds. Takes 1-2 reproduced baseline bugs;
Take 3 checked the first fixes, Take 4 reproduced concurrent control, and Take 5 verified
concurrent start and stop after correction. Exactly five Takes were created.

All five are `suspect` because both wrists are not localized in the common map. Successful
file finalization does not establish pose accuracy or all-stream completeness. This session
does not replace the previous MCAP audit or resolve its head-VIO continuity finding.

- 153 Python/system regression checks passed.
- 8 frontend regression cases passed, including ACK/read-failure separation, same/different
  task conflicts, same/stale Take stop, actual command rejection and stale task-list reads.
- Production frontend build and whitespace checks passed.
- Real UI: direct link from saved Take to its detail, five-record history, offline cache,
  reconnect, language persistence and diagnostic JSON download verified.
- Browser network injections cleared. Formal task `egohalo_4x4_static_20261029` restored,
  still 22 saved Takes, next 23; no recording left running. Original camera IPs preserved.
- Local evidence: `/home/niumu/workspaces/insight_capture_tests/customer-session-01/`.
  Includes before/after screenshots, final status/history JSON and actual downloaded
  diagnostics. The download tool experienced a long blocking wait; no download-latency
  claim is made from that interval.

## Outstanding acceptance

Real iPhone/Safari and Android/Chrome on the capture AP: connection without Internet,
lock/unlock, background/foreground, mobile network switching, WebRTC recovery and longer
sessions. The computer has no Wi-Fi adapter. Browser viewport simulation is not phone or
AP acceptance. Physical power cycle remains deferred at the user's request.

Further workflow tests should include lost mutation ACKs, task creation interrupted between
create and activate, mobile OS lifecycle interruption, long recording/thermal/storage
behavior, complete spatial calibration and replacement-camera identity. These are not
covered by the present passing results. Work remains on the field feature branch; no main
merge or customer installation package is claimed.
