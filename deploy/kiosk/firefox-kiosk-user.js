// Firefox kiosk profile prefs (deploy/kiosk/open_web_3d_right.sh). Suppresses
// first-run dialogs (Welcome tour, "restore previous session" banner,
// telemetry prompt) that would otherwise sit on top of the dashboard --
// there's no one at the keyboard on a kiosk to dismiss them.
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("browser.startup.page", 1);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("app.normandy.first_run", false);
user_pref("trailhead.firstrun.didSeeAboutWelcome", true);
// Send real-IP ICE candidates instead of mDNS-obfuscated <uuid>.local ones
// (privacy default, pointless on a kiosk viewing its own localhost backend).
// The backend can resolve mDNS candidates itself (webrtc_stream.py), but
// skipping the indirection entirely makes the kiosk's WebRTC connect
// unconditionally -- even if multicast is somehow filtered.
user_pref("media.peerconnection.ice.obfuscate_host_addresses", false);
