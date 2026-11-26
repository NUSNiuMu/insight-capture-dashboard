# Customer validation 21: isolated offline frontend reconstruction

Date: 2026-11-13, Asia/Hong_Kong. Evidence:
`/home/niumu/workspaces/insight_capture_tests/customer-session-21/`.

## Requirement and boundary

The user requires independent ownership rather than a live dependency on
ego_dashboard's source, services, configuration or state. Earlier checks built
the frontend in its working tree. This check additionally makes other workspace
repositories inaccessible and removes network access while rebuilding a clean
export. It applies to the mobile frontend, not every Jetson-native or Docker
release component.

## Actual execution

- Exported commit `644f9e3ed3e197c8e833211340917bf7ffb94a12` using `git archive`.
  The 38,891,520-byte archive contains no Git directory or installed node_modules.
  There are no tracked symlinks or submodules.
- Ran a bubblewrap mount/network/PID namespace exposing only the exported source,
  standard system tools/libraries, Node/npm, task-owned evidence and a copy of
  the pre-existing public npm dependency cache. Other workspace directories
  were absent. The only network interface was loopback.
- Used Node v26.6.0 and npm 11.18.0. Executed `npm ci --offline` with the project's
  lockfile, explicit task cache, empty user/global npm config, no audit/funding
  requests and update notification disabled. Then executed the production build
  and the existing frontend tests.
- All 37 frontend tests passed. All three generated artifacts were byte-identical
  to the committed HTML/JS/CSS, verified by SHA-256.
- A syscall trace contains no IPv4/IPv6 socket creation. The only four probes of
  `/home/niumu/workspace/...` were the test's negative visibility checks, all
  returning ENOENT. Do not describe these as zero attempted file probes.

The build still needs its normal compiler/runtime and locked third-party
dependencies. A prepared dependency cache is not a copy of ego_dashboard or a
shared project state store. This proves a disconnected reconstruction with
prepared dependencies, not dependency acquisition from an empty offline machine.
The copied disposable cache was removed afterward; the original cache, both
source repositories and the running Jetson were unchanged.

npm printed an allow-scripts warning about esbuild's postinstall. No script
approval or package-manager security setting was changed. Dependency installation,
production compilation, tests and artifact comparisons all completed successfully.

Artifacts and hashes:

See `expected-artifacts.json` and `rebuilt-artifacts.json`; the independently
rebuilt files remain under `source/web_dashboard/dist/`. The exact source archive,
`source-manifest.json`, `isolation-check.sh`, `build.log`, `build.trace` and
`validation-result.json` retain the execution evidence.

## Current physical acceptance limits

Fresh read-only field checks still show idle, formal task 22 Takes / next 23,
working three-view Android preview and valid NTP. Head VIO remains unavailable.
Only the OnePlus Android is attached and listed as a hotspot client; no actual
iPhone session was available. No recording, reboot or field code change occurred.

The active recording volume has approximately 5.68 GiB free. The system
partition independently has about 44 GiB free; it is incorrect to describe the
entire Jetson as having only 5.68 GiB. This session did not change storage targets
or authorize deleting existing recordings.

Complete head VIO/geometry calibration, wrist firmware/fault acceptance,
actual recording on the repaired clock firmware, iPhone, confirmed phone ZIP
download, long recording/storage tests and the full customer installer remain
unaccepted. Physical power removal remains deferred. This frontend reconstruction
does not replace those device checks or establish a no-bug guarantee.
