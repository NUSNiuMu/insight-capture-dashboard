# InsightCapture software and customer-flow audit - 2026-11-16

This review covers the independent field branch, the complete implemented mobile web surface, relevant backend task/recording/restart paths, and actual Jetson preview decoding. It follows the user's updated Modern Web Guidance complete-coverage requirement. Camera power remains unstable by the operator's latest report: no physical recording, camera restart, calibration motion or power-cycle was initiated during this software pass.

## Confirmed defects fixed

- A stale page could start recording under a task selected by another client. Start commands now carry the displayed task ID, checked when accepted and again under the recording transition lock before Take allocation. Historical receipts remain readable.
- Optional WebRTC codec capability/preference APIs prevented H.264-capable browsers from playing. These hints are optional; actual SDP, receiver codec and decoded frames decide readiness. Six API availability/error cases decoded the actual Jetson stream.
- A stale Settings restart confirmation could terminate active capture. Restart now checks actual recorder/finalization/merge/startup state and holds the shared recording transition lock until exit. The deployed maintenance endpoint recovered in approximately 4.1 seconds during the earlier idle software restart check.
- A stale task modal could end another client's task. Reproduced using the actual TaskRoutes/SessionTakeStore in a temporary directory: request A ended active B with HTTP200. End requests now carry the modal's original task ID and recheck it under the store lock. Missing identity requests ask for refresh; stale identity is rejected. Ending the same task is idempotent, and inactive state exposes last_task_id for exact lost-ACK reconciliation.
- Expanded preview allowed focus on the background Start button. It now uses native showModal, without a manual Tab trap or body overflow lock. The existing video panel moves into stable modal/inline hosts with feature-detected moveBefore, and insertBefore plus playback resumption in older browsers. Real decoding, same track/peer, Escape, repeated open/close, route hiding and scroll return were verified.
- Changing the task select committed immediately; empty creation forms silently disabled submission. Selection now needs an explicit submit. Creation retains native validation and reports empty/whitespace errors with associated field descriptions, correct invalid timing and focus. Native IME submission is retained; no custom Enter handler was added.
- Fixed pixel fonts ignored root text scaling; English at 320px, 200% text and increased spacing overflowed horizontally by73px. Fonts now use equivalent rem sizes, constrained grid tracks can shrink, labels wrap, and short-landscape controls scroll with the page. The failing combination now has zero document/dialog overflow. Default inline preview width is preserved.
- Trajectory role colors changed when other devices disappeared and small legend colors had insufficient contrast. Colors now derive from stable roles, with darker text and different dash patterns. This does not constitute a complete nonvisual equivalent of the trajectory dataset.

Additional improvements include route titles/heading focus, named Take detail controls, native list semantics, a keyboard bypass to main navigation, visible forced-color selection and keyboard-scrollable diagnostics. No explanatory storage copy was added: storage remains capacity plus path.

## Complete guidance review

The full catalog contains208 entries:59 applicable,149 excluded,0 undetermined. Every applicable guide and its fallback sections was read and checked. The coverage JSON and readable report remain outside the business repository. The installed checker confirms review coverage, separately reports findings/unverified items, and does not establish hardware or browser acceptance.

No experimental connection-allowlists gate was enabled or bypassed. No runtime dependency, login, IP whitelist, cross-project link, synchronization path or strict global security policy was added by this review. InsightCapture continues to own its code/resources independently.

## Evidence and validation

- Complete Python regression:306 passed; frontend regression:48 passed. Frontend build and git diff whitespace check passed.
- Actual Jetson H.264 preview: normal and missing/throwing optional API cases pass. The new modal maintains decoded frames and the same media track/peer both with moveBefore and a simulated missing moveBefore fallback.
- Six real-data browser states have zero axe violations. Manual testing is separate: it first exposed background focus despite zero initial axe violations, and verifies that background Start now cannot receive focus.
- Ten real existing-data customer flows pass: Take10/detail reload, network loss/recovery, language persistence, modal keyboard/Escape, form cancel, expanded preview, storage and baseline layout. Mutating requests were blocked.
- Fourteen browser-fixture checks pass: stale-modal identity, lost activation/end ACK, empty/missing/error/recovered Take views, literal HTML rendering, trajectory styles and starting/recording/saving/saved controls. All mutations remained intercepted inside the browser.
- 320/390px, 200% text, landscape and forced-color layout checks pass in desktop Chrome154. Additional English text spacing reproduces then clears the73px overflow. These are browser tests, not physical phone acceptance.
- A1000-Take fixture renders every record. Keyboard navigation crosses the deferred row10/11 boundary and reaches Take1000. Deferring offscreen rows reduced the longest sampled4x-CPU lab task from392ms to196ms; real phone responsiveness is still unverified.
- Default-load lab LCP around204-248ms and CLS0.093 in the latest three runs; decoded video readiness ranged418-2278ms. These are small desktop lab samples over the office path, not hotspot latency or field INP guarantees. Initial startup still contains long tasks.

## Known gaps and remaining findings

Power-dependent recording/recovery, physical camera replacement/IP changes, calibration accuracy, cold-boot RTC retention and offline clean install/upgrade are not re-certified by this pass. Actual updated iPhone Safari/Android touch, virtual keyboard, OS background/discard, VoiceOver/TalkBack and sustained performance remain unverified.

Remaining web findings are recorded, not hidden: navigation uses hash-changing buttons rather than native links; Back resets long-list position; trajectory lacks an equivalent nonvisual path dataset; some compact touch targets are40px; CSS contains old duplicate/unused rules. The prominent fixed capture controls deliberately remain available instead of hiding Stop to meet a generic content-site bar-height recommendation.

Direct private HTTP and the absence of comprehensive CSP/frame/Fetch-Metadata policies are documented deployment assumptions/gaps. Preview has an Origin check; ordinary task/settings endpoints do not have uniform Origin enforcement. This audit did not silently introduce new access/security gates or a telemetry service. It is not a server penetration test or a claim that the product has no bugs.
