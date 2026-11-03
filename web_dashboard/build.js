const fs = require("fs");
const path = require("path");
const root = __dirname;
const dist = path.join(root, "dist");
fs.rmSync(dist, { recursive: true, force: true });
for (const [source, target] of [
  ["mobile/index.html", "mobile.html"],
  ["mobile/style.css", "static/mobile.css"],
  ["mobile/app.js", "static/pages/mobile.js"],
  ["mobile/management.js", "static/pages/management.js"],
  ["mobile/presentation.js", "static/pages/presentation.js"],
  ["shared/mobile-preview.js", "static/shared/mobile-preview.js"],
  ["shared/webrtc.js", "static/shared/webrtc.js"],
]) {
  const file = path.join(dist, target);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.copyFileSync(path.join(root, "src", source), file);
}
console.log("Built phone capture page into " + dist);
