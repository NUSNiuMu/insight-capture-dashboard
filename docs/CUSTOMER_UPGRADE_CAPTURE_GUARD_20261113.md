# Customer validation 19: upgrades overlapping capture admission

Date: 2026-11-13, Asia/Hong_Kong. Evidence:
`/home/niumu/workspaces/insight_capture_tests/customer-session-19/`.

## Reproduced failure

The original installer checked recording status once, before `docker load`.
It later rewrote `.env` and stopped the old Compose project without checking
again. Running the actual script with isolated Docker/HTTP command boundaries
reproduced three paths that still reached service teardown and version change:

- Initial state idle, then recording started during image loading.
- Recording process no longer active but `merge_state` still `finalizing`.
- Backend running but recording-status HTTP request unavailable.

These are executions of the real shell script against a controlled installation
fixture, not a destructive upgrade or recording interruption on the Jetson.
The fake Docker stops execution at host synchronization, exit 73; that exit
is not the upgrade result being tested. Command logs and file contents establish
that the earlier teardown and `.env` mutation happened. No fixture command can
reach the actual Docker daemon, sudo or customer recordings.

An auxiliary baseline attempt initially failed in the reusable fake Docker
because it expected the new lock file. That attempt is retained and excluded
from failure evidence. An unlocked empty file allowed the old script, which
never reads it, to run through the intended command boundaries.

## Repair

Commit `f63b62d138c8985f709a5d4f8c56263387bb725c`:

- Capture admission takes a shared process lock before preview suspension,
  preflight and Take reservation, retaining it until recorder startup and status
  publication finish. Mobile, Qt and VIO calibration share this route.
- The installer takes an exclusive lock on the same project-owned config inode
  and holds it until exit. An in-flight start excludes upgrade; an active
  upgrade excludes new starts. Request cancellation, including repeated
  cancellation, waits for already-running startup work before releasing locks.
- Check capture status before image loading and again before `.env` mutation,
  service shutdown or migration. Include `finalizing`, operation transitions
  and a preparing/current Take. Invalid or unavailable status from a running
  backend no longer means idle. HTTP connection/overall timeouts are bounded.
- First installation seeds config from the image before creating its lock.
  Host and container can open the inode read-only despite differing UIDs.
  Process exit releases the lock; never delete or replace the empty lock file.
  It is excluded from source control and the image build context.
- Preserve the existing explicit `--force` recording-interruption override.
  It does not override a different installer or an in-flight locked start.

The lock is entirely within InsightCapture's config mount. No dependency on
ego_dashboard, LooperHub, shared source, external service or shared state store
was added. This changes upgrade/recording coordination, not ordinary readiness
thresholds or permissions to use cameras on different IP subnets.

Historical backends without this locking protocol still need to remain idle
through their first upgrade. The final status recheck catches a start during
image loading but cannot close the last concurrent-start window for those old
backends. The installer explicitly reports this limit; it is not silently
presented as atomic legacy behavior.

## Verification and deployment

129 related tests passed. Coverage includes real shell control flow and file
preservation, process-lock contention in both directions, first-install config
seeding, explicit force, failure/release paths, repeated request cancellation,
and existing mobile command, task, calibration recording, storage, recorder
failure/recovery and project structure checks. It is not full package acceptance.

Only the idle InsightCapture backend was restarted after a fast-forward of the
field feature branch from `c898c22` to `f63b62d`. Existing untracked attachments
and Compose override were preserved. The owned camera firmware, network and
calibration parameters were not modified. Fresh images, camera statuses and
required publishers returned after 5.173 seconds; this is backend/input
recovery, not a boot or complete recording-readiness measurement.

On the real Jetson:

1. Host and running container lock paths were verified to be the same inode.
2. With the host holding the exclusive lock, recording start returned HTTP 409
   with `系统正在升级，请等待升级完成后再开始录制。`.
3. After release, the same request returned the ordinary HTTP 422 preflight
   result. A deliberately absent QA `/camera/imu` topic guaranteed no real
   recording could begin even after lock release.
4. Current Take and task counts were unchanged. The formal task remains
   22 Takes, next 23, idle. Protected formal/QA metadata hashes and MCAP
   size/mtime snapshots match before/after; not every data file was fully hashed.
5. Three runtime-file hashes match the candidate. Final NTP, image freshness
   and required publisher checks pass; head VIO remains `NOT_INITED`.

The real Android page was not refreshed or touched. Preview automatically
reconnected after the backend restart and subsequent rejected preflight. A
post-recovery same-SSRC 42.763-second window added 1,283 decoded frames
(30.002 fps), zero RTP-loss/freeze increments and two presentation drops.
Do not infer exact phone interruption latency from these snapshots.

## Remaining delivery and field work

Full offline installer/upgrade acceptance remains open. Inspection also found
that `update.sh` copies the hotspot sources into the installation but does not
refresh `/usr/local/libexec/insight-capture-hotspot` and related host services;
`install_camera_discovery.sh` only installs discovery/chrony. New clean-host
hotspot setup and propagation of host hotspot repairs need a separate verified
delivery change; do not claim field service updates imply the package does it.

The broader physical blockers remain: head illumination/VIO and complete
geometry calibration, both wrist variants' clock repair validation, real iPhone,
final phone download acceptance, adequate space for long recording/storage
fault tests, and deferred physical power interruption. No complete installer
was run on the field installation, no customer package was built, and there
was no main merge or push.

Primary evidence: `baseline-result.json`, `baseline-other-states.json`,
`tests.xml`, `deployment-result.json`, `field-lock-result.json`,
`runtime-hashes.json`, `validation-result.json`, data snapshots and phone
screenshots/statistics. The two-page PDF separates fixture, actual Jetson API,
actual Android preview and outstanding full-release acceptance.
