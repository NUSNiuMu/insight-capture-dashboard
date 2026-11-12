# Real phone preview visibility and navigation

2026-11-13, Asia/Hong_Kong. Source and deployed frontend: `5dae44c`.
Continuation of real customer-path validation, not a complete product sign-off.

## Findings and correction

The physical OnePlus 7T Pro (HD1910, Android 11, Chrome 132) was available through
the computer's authorized ADB server on port 5039. Screenshots and Chrome video
statistics confirmed that the initial capture page was receiving live video,
but duplicated task/status cards pushed almost all of the overview beneath the
fixed recording dock. This observation does not retrospectively explain every
earlier report of a missing page or missing video.

The capture header is now compact. Ready cameras remain listed on the Device
page; cameras needing attention still appear directly on the capture page.
Failures and storage warnings stay visible. Task selection moved beside the
existing task/Take information in the recording dock. The Take number does not
get truncated along with a long task name.

Preview size follows the available viewport, measured from the independent
header/status/control area and the actual footer height. Scrolling does not
resize it based on scroll position. The complete video and matching click
regions scale together without cropping. A minimum card height of 280 CSS px
keeps the picture usable; very short screens or several warnings still require
scrolling, and the expand button remains reachable. Landscape expanded preview
places the controls in a scrollable side panel instead of three rows above the
video. The original full-screen control remains available.

## Physical Android evidence

- Loaded JS asset `6664dc97e7a3` from `http://10.77.0.1:8765/mobile`.
  Page, JavaScript and stylesheet bytes match the locally built artifacts.
- In a 384 x 703 CSS-pixel viewport with the browser address bar visible, the
  1280 x 1280 overview is fully visible above the recording dock; its rendered
  square is about 176 x 176 CSS px. With browser chrome collapsed, the available
  viewport is taller and the square grows to 226 x 226 CSS px.
- In the same ordinary landscape viewport (832 x 255 CSS px), the rendered
  overview height increased from 99.19 to 255.19 CSS px. System fullscreen
  provides about 363 CSS px of video height on this phone.
- Actual touch input passed: task selector open/close without changing task;
  center camera 1088 x 1920; left and right wrist 544 x 640; overview; tapping a
  tile to enlarge; close returning to overview; rotation; system fullscreen;
  Android Back leaving fullscreen; Chinese/English and return to Chinese.
- Capture -> trajectory -> Device/Records -> capture passed. The phone had
  zero connected WebRTC peers on the non-preview pages; returning to capture
  decoded video again. The trajectory page correctly showed only the head
  localized, while both wrists still awaited localization.
- Final selected candidate pair: phone `10.77.0.106:38632` to Jetson
  `10.77.0.1:8768`, UDP. The computer is the debug/control channel, not the phone
  media path. The final roughly 123-second observation had 30 FPS, 0 reported
  network packet losses and 0 reported freezes. Browser presentation reported
  30 dropped frames out of 3,716 total; this is not a zero-frame-drop claim.

The phone's prior QA download was cancelled at Chrome's warning, without
choosing Keep or changing browser security settings. Download acceptance is
still incomplete. The phone is left in Chinese, portrait, capture overview;
the original rotation settings (0/0) were restored.

## Additional checks and scope

33 existing frontend tests, 10 project-structure tests and the build passed.
Desktop Chrome exercised seven viewport configurations, including 320 x 568,
360 x 640, the physical phone's portrait dimensions, larger phones, landscape
and desktop. No horizontal page overflow or JavaScript exceptions were observed.

Six additional desktop checks used synthetic status responses: starting,
recording, saving, multiple warnings, scrolling warnings to expand/close the
preview, and no active task. They verified disabled task switching while busy,
preview pause/expanded-view close, stop-button availability during recording,
start disabled during preparation/save/blocked preflight, and task selection
when no task is active. They sent no command POSTs. These are UI-state checks,
not new physical recordings or new recording-lifecycle acceptance.

The first local asset harness intercepted the main HTML and therefore lost
Chrome's private-address-space classification. Its API calls were blocked by
the browser. The corrected harness loads real device HTML and substitutes only
the locally built JS/CSS; it passes without disabling browser security. A second
harness initially read an absent conditional node during a state transition;
the null-safe wait was corrected. Neither harness failure was counted as a
product failure or a successful check.

## End state

Frontend was deployed by a fast-forward of the field branch. Backend container
PID remained `1259066`; no backend restart, recording, calibration reset or
network change was needed for this update. The initial bundle fetch named a
branch not exported by the HEAD-only bundle; no remote checkout changed until
the exported HEAD was fetched and verified.

The formal task remains `egohalo_4x4_static_20261029`, 22 Takes, next Take 23,
idle, preflight ready. Protected formal Take 22 and QA03 Take 1 metadata hashes
and MCAP size/mtime match the pre-test snapshot. No recordings were deleted.
Fallback recording storage has about 5.6825 GiB free and its warning remains
visible. No main merge, push, or customer installer rebuild was performed.

Evidence directory:
`/home/niumu/workspaces/insight_capture_tests/customer-session-11/`.
Key files: `phone-current.png`, `phone-portrait-after.png`,
`phone-landscape-before.png`, `phone-landscape-after.png`,
`phone-final-steady.json`, `browser-layout.json`, `browser-ui-states.json`,
`frontend-deployment.json`, `formal-data-before.json`, `formal-data-after.json`.

Real iPhone, Chrome download confirmation, physical power removal, complete
spatial calibration, long recording/endurance and the head VIO cadence issue
remain open. This frontend-only change does not resolve those items.
