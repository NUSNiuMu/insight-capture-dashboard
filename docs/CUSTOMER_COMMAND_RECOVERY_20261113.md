# Lost command replies and backend restart recovery

2026-11-13, Asia/Hong_Kong. Follow-up to the real Android acceptance finding
where a saved/online page still displayed an unconfirmed-operation message.

## Confirmed causes and correction

The mobile status API exposed only the latest operation. A phone could lose its
start response, another client could stop capture, and the first phone would
then have no read-only way to identify its own accepted start. The latest 100
receipts existed only in memory. An isolated test of `5a1fb79` also reproduced
the same start ID allocating a second Take after backend reconstruction:
start calls `[1, 2]`, output `/take/1 -> /take/2`.

`721b611` commits each accepted command to the project's own
`outputs/results/mobile_commands.sqlite3` before scheduling its action. Exact
request IDs and action/output-path fingerprints survive process restarts and
are not evicted after 100 subsequent requests. A new GET endpoint,
`/api/mobile/commands/{command_id}`, provides the original receipt independently
of the latest operation. The page reads this receipt to resolve its cached
pending command without repeating a POST.

Startup converts committed but unfinished receipts to `interrupted`, with an
explicit unknown-result message. This does not claim that the recording failed
or succeeded; it prevents automatic replay. A completed operation remains
complete even if persisting its final receipt fails; the failure is logged and
the earlier durable receipt still prevents a second start. Accepted operations
finish scheduling even if the HTTP request is cancelled during the commit.

`73de8fa` publishes received device status immediately while an old receipt is
being checked. A delayed history lookup must not hide current connection state.
This is a consistency change, not a new permission or calibration gate. A new
command must be able to store its receipt in the existing project results
directory before it executes. Receipts lost before this version was deployed
cannot be reconstructed automatically.

## Validation

- The old duplicate-start behavior was reproduced in an isolated route harness,
  not by intentionally creating a duplicate formal recording on the device.
- 139 runtime/web/deployment tests passed; 33 frontend tests and the build passed.
  New cases cover delayed/overwritten ACKs, backend restart, more than 100 later
  commands, changed fingerprints, receipt write failure, cancelled acceptance,
  unknown interrupted results, and delayed receipt lookup.
- Real Jetson: send a QA start but deliberately never read its HTTP response;
  stop/save through a second client; query the original start while the latest
  operation is stop; restart the idle backend; resend the exact original start.
  It returns the original completed receipt and original Take path. The task
  remains at four Takes and no new recording starts.
- The initial raw socket probe disconnected before application acceptance and
  was not counted as the field test. The accepted run retained the socket until
  the receipt was visible to an independent GET, then discarded the response.
- A separate desktop Chromium context was seeded with that actual pending
  command, then loaded the device page after restart. It issued only GET requests
  and no mobile command POST, removed the pending identity/error, and rendered
  1280x1280 preview. A 1.8 s initial status delay exposed the initial pending UI
  for the before screenshot. This is desktop browser verification; the physical
  Android remains at its earlier browser download confirmation.

The only new recording is QA phone Take 4, actual MCAP span 2.872311132 s,
5,395 messages, zero empty messages and zero audited source/record epoch
mismatches. Deep QC still fails head VIO: 98.08 Hz against 99 Hz, estimated
deficit 0.70%, one 18 ms gap. No quality thresholds changed.

## End state and evidence

Field source `73de8fa`; UI asset SHA-256 prefix is recorded in
`customer-session-10/served-assets.json`. The SQLite integrity check returned
`ok`; it contains the two completed QA command receipts and is 12,288 bytes.
The formal task is restored to 22 Takes, next 23, not recording, preflight ready.
Protected formal Take 22 / QA03 Take 1 metadata hashes and MCAP size/mtime remain
unchanged. All QA data is retained; fallback recording storage has about 5.68 GiB
free. The QA-only duration guard ended without firing. No further recording was
started to test the UI.

Local evidence: `/home/niumu/workspaces/insight_capture_tests/customer-session-10/`.
Key files: `before-duplicate-reproduction.json`, `field-command-replay.json`,
`command-database.json`, `browser-receipt.json`, `take4-direct-mcap.json`,
`take4-integrity.json`, `formal-data-after.json`, and the regression logs.

The Chrome download warning, real iPhone, physical power removal, spatial
calibration and long-duration/source-VIO work remain outside this acceptance.
No main merge, push or customer installer rebuild was performed.
