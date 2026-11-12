# Customer validation 20: propagate owned host hotspot updates

Date: 2026-11-13, Asia/Hong_Kong. Evidence:
`/home/niumu/workspaces/insight_capture_tests/customer-session-20/`.

## Delivery defect and change

The release installer copies the current deploy sources from its image, then
calls `install_camera_discovery.sh`. Previously that entry point only installed
discovery/chrony; it did not update the hotspot supervisor, DHCP configuration
or isolation service in their actual host runtime paths. Existing field files
already matched the repaired source, which did not prove future packages
would propagate those repairs.

Commit `fcf3cf214132d2308fc8c3728a5a045a6eb303d3` adds one standard-library
`install_network_services.py` used by discovery upgrades, initial driver setup
and explicit old-network migration. Existing configured hotspots are detected
from their owned UPLINK configuration; an ordinary upgrade does not invent or
rewrite a NetworkManager profile.

- Validate host dependencies, source syntax and chrony/dnsmasq configuration
  before replacing owned files. Copy only differing bytes/modes, using atomic
  file replacement. Preserve the old chrony config when it changes.
- Enable the selected services; start inactive ones, and refresh only those
  with changed files. Recheck dependency state after restarting chrony so its
  discovery dependent is restored even when discovery's files did not change.
- Add `ExecReload` for the isolation oneshot. Reload rules while it remains
  active instead of restarting a Required dependency and stopping the AP.
- Keep driver binaries, driver options, site SSID/password/connection settings,
  DHCP leases and discovery's completed startup checkpoint. Identical installs
  leave running services and files alone.
- Initial driver preparation installs/enables services but defers starting them
  until reboot. Explicit old-network migration still changes network setup;
  the ordinary release-upgrade path does not invoke that migration.
- Retire the obsolete automatic camera OS reboot unit without stopping a job
  already running. No new cross-project source, service or state dependency.

## Controlled verification

51 installation and related regression checks passed, including differences,
idempotence, missing dependencies/profile/source, invalid DNS config, delayed
first startup, inherited symlink replacement without changing its target,
dependency recovery and the previous upgrade/capture admission protection.
The tests operate on temporary files and simulated systemctl boundaries.
Their count overlaps session 19's checks and is not an added acceptance total.

## Actual Jetson application

The field branch advanced from `f63b62d` to `fcf3cf2` without restarting the
capture backend. Existing untracked attachments and Compose override remained.
An independently connected USB SSH session ran the actual installation entry
point, holding the capture-upgrade lock while the formal task was idle.

Three paths were verified:

1. Initial application changed only the isolation unit to add ExecReload,
   then reloaded it. All resident service PIDs remained unchanged.
2. A controlled stale-file fixture appended one harmless comment to the
   installed hotspot script. No old network logic was introduced. The installer
   restored the authoritative script and restarted only the hotspot supervisor.
   DHCP, chrony and discovery retained their PIDs. A finally handler restored
   the owned script if needed; the successful path needed no failure recovery.
3. Repeating the installation produced zero changed files, zero starts,
   zero restarts and zero reloads. All service states/PIDs remained unchanged.

Before/after hashes match for the capture NetworkManager profile (including
its site credentials), the driver and its options, UPLINK config, DHCP leases
and discovery startup checkpoint. Password values were not read out or stored
in QA evidence. AP addresses, active profile and the complete `iptables -S`
rules match. All five selected services finish active and enabled.

The actual Android phone kept the same WebRTC SSRC across the operations:
22.321 seconds, 670 additional decoded frames, 30.017 fps, zero additional
RTP lost packets or freezes, and one additional presentation drop. This finite
window is not an endurance or all-scenarios connectivity claim. No phone
refresh or touch intervention was used.

Final NTP, three image inputs and required publisher checks pass. Head VIO
remains `NOT_INITED`. No Take was created/deleted; formal task remains idle,
22 Takes, next 23. Protected formal/QA metadata and MCAP size/mtime snapshots
match. This is not a hash audit of every recording or a camera firmware update.

## Remaining acceptance

The host-file propagation defect identified in session 19 is now addressed for
an already-provisioned capture hotspot. The full self-extracting customer
installer has not been built/run here, and clean-host installation still
requires matching kernel/driver and prepared host network dependencies. The
application installer does not choose site credentials. Package migration,
full clean installation and physical cold power acceptance remain distinct.

Head illumination/VIO initialization, complete spatial calibration, wrist clock
fault verification, real iPhone, final phone download and long recording/storage
tests retain their previous limits. No main merge, push or customer package.

Evidence: `deployment.json`, `field-result.json`, `field-test.log`, `tests.xml`,
`validation-result.json`, before/after data snapshots and phone screenshots /
WebRTC statistics. The existing upgrade report now includes this as page 3.
