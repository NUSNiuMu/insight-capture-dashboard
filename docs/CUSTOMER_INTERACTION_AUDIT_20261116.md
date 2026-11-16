# Customer interaction audit - 2026-11-16

Scope: independent InsightCapture field branch based on `57aaeb9`, deployed on the Jetson. This pass combines production route tests, actual browser interaction with the device and live H.264 decoding. Camera power was previously reported unstable; no new physical recording, camera restart or power-cycle was performed in this pass.

## Fixed: a stale phone page could record under another task

Reproduction: phone A still displays task A; client B selects task B before A's next status poll; A presses Start. The old phone request did not carry its displayed task ID, and the backend accepted it under B. An isolated reproduction with the real MobileCaptureRoutes, RecordingRoutes and SessionTakeStore confirmed this behavior without hardware recording.

The phone now includes its displayed task ID. New start commands validate it at acceptance and again under the recording transition lock before preflight and Take reservation. A task change during receipt persistence also prevents recording. The persisted receipt includes the task ID; reusing a command for another task is rejected while reading an original historical receipt remains valid. Existing recorded data and receipt schema are preserved. An outdated page without a task ID is asked to refresh.

Regression coverage: missing/invalid task identity, task changed by another client, task changed during receipt commit, historical receipt after task switching, command ID reused for another task, and the actual frontend request payload. Unrelated native recording entry points retain their existing behavior.

## Fixed: optional browser APIs could prevent working H.264 preview

The old player required both RTCRtpReceiver.getCapabilities and RTCRtpTransceiver.setCodecPreferences to succeed. In a real Chrome browser, hiding either API produced zero decoded frames even though the browser supports H.264.

The player now treats those APIs as hints. It validates H.264 in the offer, validates the answer, checks the negotiated codec in receiver statistics and declares readiness only after decoded frames arrive. A non-H.264 offer/answer/receiver codec and a connection with no decoded frames still fail explicitly. Transport, encoder parameters and recording-time preview suspension remain unchanged.

Live device validation used desktop Chrome 154.0.8037.57 with a 390 x 844 mobile viewport. Normal behavior, missing/throwing capability query, empty capability list, and missing/throwing codec preference API all decoded the actual Jetson 1280 x 1280 stream as video/H264. These are browser API fault-injection results, not six different physical phone models.

## Customer workflow walk-through

The browser used the real device APIs and existing QA Take 10. All mutating requests were blocked at the browser boundary for this walk-through.

- Open the saved Take detail and confirm its 31-second MCAP data span.
- Disconnect the browser network: the loaded Take remains readable and the page indicates offline state.
- Reconnect: the page returns to authoritative online state.
- Reload a direct Take detail URL: the selected record and duration are restored.
- Switch Chinese/English, reload, then switch back: language state persists.
- Open the task modal, navigate with the keyboard and dismiss with Escape.
- Fill a new task form and cancel: no write request is issued.
- Expand preview and close with Escape: the inline view returns.
- Check the storage card shows capacity and path, with no extra storage description.
- Check 390-pixel page width for horizontal overflow.

No browser page errors were observed in this walk-through. A suspected trajectory connection leak was checked and disproved: leaving the trajectory view unmounted it and closed its WebSocket. Tests of delayed task reads waited for the modal to actually open; an early sampling error was a test-script issue, not a confirmed product failure.

## Guidance review and evidence

The requested modern-web-guidance skill was used to review task dialogs, forms and asynchronous interaction. Its dialog/forms guides were read. No applicable WebRTC codec guide was found after focused searches; codec handling was checked against the API documentation and live browser results instead.

Related regression results: 117 backend tests and 48 frontend tests pass; frontend build and git diff whitespace checks pass. The API compatibility tests also cover non-H.264 rejection and decoded-frame timeout. Evidence snapshots and browser results are retained locally under `.local/insight-customer-audit-20261116` in the local ego-system project group and are not runtime dependencies.

## Still requiring field validation

Stable-power recording and interruption recovery, RTC retention across power loss, physical camera replacement and calibration accuracy, and longer sustained use still require their corresponding physical conditions and checks. This pass does not establish that the whole product is bug-free or replace those acceptance items.
