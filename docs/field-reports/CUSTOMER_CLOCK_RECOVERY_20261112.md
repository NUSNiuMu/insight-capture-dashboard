# Customer workflow: clock readiness race and truncated MCAP recovery

Field code: `7f10784`. This follows the discoveries in
[`CUSTOMER_RECORDER_FAILURE_20261112.md`](CUSTOMER_RECORDER_FAILURE_20261112.md); that report remains a historical snapshot.
Only the InsightCapture branch and its owned camera clock adapter were changed.
The camera acquisition firmware and system ROS libraries were not replaced.

## Clock-domain evidence and repair

The QA07 Take 1 raw CDR headers contain 661 wrong-domain timestamps across nine
head topics, during approximately one second. The head publisher journal records
`NTP sync lost. Timestamps revert to CLOCK_MONOTONIC` at 20:48:41.340 and acquisition
of the offset again at 20:48:42.343, matching the recorded transition.

The deployed owned clock adapter rewrote `is_time_sync` with `Path.write_text`
every two seconds. A reader can observe the file after truncation and before the
new bytes are written. A subsequent 100-second read-only probe saw four empty
reads among 10,121 flag reads. Local LooperHub source was inspected read-only to
understand the consumer; its current checkout is not proof of deployed firmware
identity. The real publisher log independently confirms the domain switch.

The adapter now writes a complete temporary file in the same directory and uses
atomic replacement; unchanged values do not rewrite storage. Genuine readiness
loss still writes zero. Tests cover complete old/new visibility, unchanged inode
and mtime, failed replacement preserving the old flag, and malformed old contents.
No NTP bounds were relaxed, and no clock-setting command was introduced.

All three cameras now have script SHA-256
`f5c67a5da0c795f6800e1f81190a9095f285328bc4cc5e741615768b2c9c3bff`.
Only their owned clock service was restarted while recording was idle. A status
collection timed out after updating the head; the retry verified its new hash and
did not restart that camera's clock service again. Final camera/API checks passed.
A second 100-second head probe saw 10,452 complete `1` flag reads and 36,126 IMU
messages, with no wrong-domain or backward timestamp events.

The original incident's precise file-read interleaving was not recorded. The
race is independently demonstrated, and the journal matches the timestamp-domain
symptom; this is not a claim that all future clock faults are eliminated. Actual
NTP loss can still trigger firmware domain switching. The Jetson NTP measurement
error wording also requires investigation for scheduling-induced false positives.

## Interrupted file recovery

The old workflow reindexed the source copy and immediately used `AnyReader` to
validate it. Reindexing creates metadata but does not repair the truncated MCAP
footer, so the source was rejected before conversion with `File end magic is
invalid`. The existing ROS storage reader can scan readable records from this
file, and `ros2 bag convert` produces a complete output.

Recovery now checks readable source counts against reindexed metadata, converts,
strictly validates the completed output, and compares types, counts, timestamps
and serialized payload fingerprints. Fingerprints sum each message's SHA-256
modulo 2**256 per topic, retaining multiplicity while permitting index order
changes. Data is not deserialized, repaired, or rewritten in the original file.
The manifest declares `recovery_scope: readable_messages_only` and warns that
incomplete tails and unwritten messages cannot be recovered.

Before deployment the full new recovery class was exercised on an independent
copy of QA07 Take 2. It recovered 14,549 messages; the archived copy and production
original retained their bytes, sizes and mtimes. Then an idle backend service
restart recovered the two explicitly inventoried QA fault samples:

| Sample | Recovered messages | Original verification |
| --- | ---: | --- |
| QA07 Take 2 | 14,549 | SHA-256, size and mtime unchanged |
| QA07 Take 3 | 49,266 | SHA-256, size and mtime unchanged |

Both recovered bags are listed by the bag API. Originals reside in hidden
`.original` directories under staging; recovered copies have `_recovered` names.
The failed Take records remain failures. A direct recovered-file link in mobile
task history is still pending and must not be presented as already delivered.

## Validation and end state

- 199 backend/system tests passed, including 10 new clock/recovery regressions.
- The frontend was unchanged; its prior 25-test/build verification remains the
  applicable frontend evidence, not a new mobile-browser acceptance run.
- Formal Take 22 and QA03 Take 1 metadata/manifest/Take hashes and timestamps,
  plus MCAP sizes and timestamps, match the preceding session.
- Formal task still has 22 recordings; next is Take 23. No recording is active,
  all cameras and preflight are ready. The existing fallback NVMe has about
  6.87 GiB free after recovery copies. No recording data was deleted.
- Source is committed/deployed on the field feature branch, not main, a remote
  push, or a customer installation package.

Evidence is retained under local `customer-session-08/`: publisher journals,
before/after flag and IMU probes, scripts/hashes, private-copy recovery, deployed
recovery checks, data fingerprints, API snapshots, screenshots and PDF.

Remaining customer validation includes distinguishing wrong clock domains from
rate-shortfall counts, mobile recovery-file access, real NTP-loss behavior,
iPhone/Safari and Android/Chrome over the hotspot (including background/lock),
full calibration, and longer thermal/storage tests. The PC USB connection is
not a phone Wi-Fi test. Physical power-cycle testing remains deferred as requested.

Format reference: https://mcap.dev/spec (footer/magic requirements).
