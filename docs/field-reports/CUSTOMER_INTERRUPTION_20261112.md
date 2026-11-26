# Customer workflow validation - interrupted operations, session 02

Baseline `378728d`. Real Jetson and three cameras; browser UI through USB. The tests
distinguished a request blocked before delivery from a successful response lost after
the server committed its result. No formal recording was made or modified.

## Reproduced problems and resulting behavior

1. Lost task-create ACK followed by retry created two same-name tasks with different IDs.
   The browser now retains one creation identity and its original payload, queries that
   exact task after a failed request, and resumes activation. A draft survives reload in
   the same browser tab when session storage is available. Creation and activation are
   not automatically replayed on reconnection; continuing requires the user's action
   when the previous attempt could not be resolved.
2. Lost capture ACK left `Failed to fetch` visible while the device was recording. Pending
   command identity is now checked against the device operation. Unknown outcomes are
   explicitly marked unconfirmed and reconciled on reconnect, without automatically
   starting another recording. Retrying the same still-unconfirmed command reuses its ID.
3. Offline capture footer showed stale “ready” and zero duration. It now shows unconfirmed
   state and an unknown duration instead of presenting the cache as current.

Real stop/save testing then exposed a race in the first candidate: a poll acknowledged the
command before the POST response failed, so the later error overwrote the acknowledgement.
Final logic waits for request handling to settle before retiring the command identity.
The real repeated test withheld the stop response until the 12-second client timeout;
polls saw the saved result first, and the final page retained the confirmed save with no
unconfirmed-result alert. This candidate failure is retained in evidence, not counted as
a passing test.

Unfinished creation offers return to the task list and an explicit exit from setup. Exit
only clears the browser's draft; it does not delete any task already created on the device.
A pre-delivery request block verified that exiting does not create a task or change the
active formal task. This prevents recovery state from trapping the user in the form.

## Real test sequence

- Baseline task name `QA 客户使用走查 02 · 断网确认`: one POST returned 201 but its response
  was dropped. Retry created two IDs (`task_106cf769a20163d0cacd9946ffb8be85` and
  `task_ecb94cfbeeeb02cea3bc3475a85446ca`). These QA evidence tasks were retained.
- Corrected create and activate each lost their successful response: exactly one task
  `task_8ee35262cb34cf7379c25abd30e33357`, active and no false failure.
- Full API disconnection after a committed create, reload, retry, blocked activation,
  another reload, and explicit continue: exactly one task
  `task_cf5f7370c9ae45fd3b2b9c7f9bb86fb9` and one task directory.
- Start ACK lost followed by full API disconnection and page reload: reconnection showed
  the original Take 1 running; normal stop finalized it.
- Stop ACK loss and earlier poll acknowledgement: first candidate reproduced the race;
  corrected repeat retained the final saved Take 2 after timeout and reconnect.
- Own recordings only: 10.07, 22.09, 20.47 and 26.39 seconds, all finalized. Wrists remain
  unlocalized, so saved files do not constitute spatial-quality acceptance.

## Verification and remaining boundaries

18 frontend regression cases passed, including 10 new recovery/race cases; the four
existing console integration checks passed, production build and diff checks passed.
Backend implementation was unchanged in this session; the previous 153-check backend/
system result is not relabeled as a newly executed full suite.

Network injections were cleared and formal task `egohalo_4x4_static_20261029` restored:
22 Takes, next 23, no active recording. All camera/clock preflight checks passed; free
storage was about 29.87 GiB after these QA captures. No project coupling or new capture
gate was introduced, and no quality threshold changed.

Evidence: `/home/niumu/workspaces/insight_capture_tests/customer-session-02/`.
Real iPhone/Android AP behavior, browser-process termination, different devices sharing
an IP address, physical power loss, full spatial calibration, head-VIO continuity and
long-duration thermal/storage behavior remain separate acceptance work. Session storage
provides same-tab reload recovery; it is not a durable server operation journal and does
not claim recovery after browser data is cleared or an unrelated new tab is opened.
