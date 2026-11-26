# Customer validation 16: consolidated acceptance and camera shutdown repair

Date: 2026-11-13, Asia/Hong_Kong. Evidence:
`/home/niumu/workspaces/insight_capture_tests/customer-session-16/`.

## Current outcome

The real Android phone is accessible over the computer's authorized USB ADB
connection (server 5039). The page and selected WebRTC media path remain phone
`10.77.0.106` to Jetson `10.77.0.1:8768`; there is no ADB reverse. The phone is a
OnePlus 7T Pro HD1910, Android 11, Chrome 132. A screenshot and live browser
statistics confirm overview playback, not merely an HTTP response.

The head camera now retains a **shutdown-only** library repair. InsightCapture
runtime remains `6aa8a73`; no backend code or network configuration changed in
this session. Head VIO remains `NOT_INITED`, so full capture preflight is not
ready. This is not full customer acceptance or a repaired VIO cadence claim.

## Independent camera firmware change

Separate LooperHub worktree commit `13cac20908e6d764e5868a0ab8385ef144b088f2`.
Relative to the byte-identical reproduced baseline `d73f7ad`, the final source
diff is only 11 added destructor lines in `insight_full_node.cpp`: stop, wake,
clear the pending request and join the FPS worker before pipeline teardown.
`insight_full_node_vio.cpp` is byte-identical to the reproduced baseline. The
previous unaccepted VIO scheduling change is absent from this deployed library.

- Original SHA-256: `7827cad98342c2d3c2983492e7580fa98f0540014820014d57045a79cbe4a907`.
- Installed SHA-256: `f6af48e15dc5942ef6495b0c0978a94b35821f17c3d43836986731110cef2dd5`.
- Build ID: `e62bb075371d68667c812ebde146f366b0f7942b`.
- No new undefined dynamic symbols or removed exports; no class-header change.
- Original and candidate share the pre-existing isolated RTLD_NOW failure for
  `avformat_close_input`; RTLD_LAZY succeeds for both. This remains a release
  linking audit item, not a newly introduced candidate dependency.
- Actual process mappings identify the installed library inode. The final
  service PID is 43931, active, no automatic restarts at the recorded snapshot.
- Provenance is stored at
  `/userdata/install/lib/libinsight_full_plugin.so.field-fix.json`.
  Existing software version/source marker files were preserved; their old
  values alone no longer describe this field library patch.

This firmware work creates no InsightCapture runtime/build/upgrade dependency
on LooperHub or ego_dashboard. Production-directory scans found no such path or
sync reference, and Git has no tracked symlinks. Existing project-local Fast DDS
SONAME links are internal library aliases, not cross-project source sharing.
No main merge, push, customer package or clean-install acceptance occurred.

## Actual stop/start validation

| Final validation sequence | Result | Measured time |
| --- | --- | ---: |
| Original idle ROS stop | SIGABRT 6, new crash file | 4.3890 s |
| Shutdown-only candidate stop 1 | success, process gone, no new crash | 4.8886 s |
| Shutdown-only candidate stop 2 | success, process gone, no new crash | 4.9632 s |
| Candidate starts 1 / 2 / 3 | fresh head image and native status | 16.4505 / 16.4323 / 16.4607 s |

Stop times surround only `systemctl stop` on the camera. Start times include
NTP startup and input initialization, not Jetson boot or full recording readiness.
Both final stop journals contain `FPS switch thread stopped`. Current mode is
ROS 1. Actual FPS request production is in UVC COMMIT handling; concurrent UVC
reconfiguration was not tested or induced by changing USB mode.

An earlier attempt in this session already stopped the candidate successfully,
but the harness incorrectly required that text in the ROS log file alone. It
automatically restored the original. `attempt-1/` preserves the full evidence.
The corrected harness saves both journal and ROS logs and verifies service
result, exit status, process disappearance and new crash files. The final two
stops passed those checks; this correction did not change firmware or quality
thresholds. Do not silently omit the instrument-induced rollback.

All 13 protected configuration/version hashes, including seven calibration
files, match before/after. Protected formal and QA recording snapshots also
match. No new Take was created or recording deleted. Formal task still has
22 Takes, next 23, idle. Final NTP is fresh and passes all three device checks;
required publishers are present. The only final admission failure is missing
head VIO. Three images and wrist VIO are fresh; main disk is still absent and
fallback NVMe has about 5.68 GiB free.

The real phone stayed on its existing overview stream. After the final camera
start, a 20.449-second same-SSRC window added 613 decoded frames (29.977 fps),
zero RTP-loss/freeze increments and one presentation drop. This is finite
post-recovery evidence, not zero loss over the whole session or proof that the
head region did not pause during camera stops.

## Consolidated acceptance ledger

The seven-page Chinese PDF separates historical evidence from current state:

- Dynamic IP: left wrist really changed `.50.1/.50.2 -> .40.1/.40.2`, reusing
  the old head subnet after moving head to `.60`; 44.50 s to ready. Another
  private `.17/.94` pair passed. See [`MOBILE_EDGE_ACCEPTANCE_20261112.md`](MOBILE_EDGE_ACCEPTANCE_20261112.md) and
  [`MOBILE_PREVIEW_ACCEPTANCE_20261111.md`](MOBILE_PREVIEW_ACCEPTANCE_20261111.md). Physical replacement remains distinct.
- Changed-IP warm reboot: USB 52.56 s, API 58.93 s, inputs 108.87 s, complete
  readiness 115.79 s. These do not establish physical power-loss behavior.
- Real Android/offline: phone Take 3 started and stopped with no Jetson default
  routes, 7,328 MCAP messages, no empty/wrong-epoch records. Head VIO QC failed.
  See [`CUSTOMER_ANDROID_ACCEPTANCE_20261113.md`](CUSTOMER_ANDROID_ACCEPTANCE_20261113.md).
- DDS empty-message repair, recorder death detection, atomic clock flag,
  interrupted-file recovery, durable command receipts, live warnings, phone
  layout/native VIO status and live-node discovery are covered in sessions 06-15.
  Historical test counts overlap and are not summed into a synthetic total.
- Restriction ownership: fixed IP/MAC and unnecessary OS reboots removed;
  preview pause and isolated offline acquisition are explicit requirements.
  NTP 10/20 ms, RTT 10 ms, five stable startup checks and 100 ppm are engineering
  choices; several bounds overlap. Existing fixed 5 GiB free-space policy is
  conservative and should be reviewed against duration and write rate.

Pending: controlled illumination/VIO initialization and cadence, complete
spatial calibration, another physical camera and calibration identity, actual
NTP-loss behavior, real iPhone, final phone download/file verification, long
thermal/drift and real storage-failure acceptance, physical cold power (deferred),
and the full independent customer installer/upgrade. The longest Take in this
validation series is about 151 seconds, not an endurance result.

PDF:
`/home/niumu/文档/项目资料/Insight_数采背包/测试报告/InsightCapture_客户使用深度验证总报告_2026-11-13.pdf`.
Core evidence: `validation-result.json`, `shutdown-before.json`,
`original-stop.json`, `candidate-stop-1.json`, `candidate-stop-2.json`,
`candidate-start-*.json`, `shutdown-final.json`, `field-fix-manifest.json`,
`before-data.json`, `after-data.json`, `final-runtime.json`, `phone-final.png`,
`phone-final.json`, `phone-window-end.json` and `attempt-1/`.
