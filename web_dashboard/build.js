const fs = require("fs");
const path = require("path");

const root = __dirname;
const srcDir = path.join(root, "src");
const distDir = path.join(root, "dist");
const staticDir = path.join(distDir, "static");

function resetDir(target) {
  if (fs.existsSync(target)) {
    fs.rmdirSync(target, { recursive: true });
  }
  fs.mkdirSync(target, { recursive: true });
}

const PRESERVE_STATIC = ["babylon.js", "babylonjs.loaders.min.js"];

function copyFile(source, target) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(source, target);
}

// Preserve large vendored files that are not rebuilt from source
const preserved = {};
for (const name of PRESERVE_STATIC) {
  const p = path.join(staticDir, name);
  if (fs.existsSync(p)) preserved[name] = fs.readFileSync(p);
}

resetDir(distDir);
resetDir(staticDir);

for (const [name, buf] of Object.entries(preserved)) {
  fs.writeFileSync(path.join(staticDir, name), buf);
}

copyFile(path.join(srcDir, "index.html"), path.join(distDir, "index.html"));
copyFile(path.join(srcDir, "3d.html"), path.join(distDir, "3d.html"));
copyFile(path.join(srcDir, "cameras.html"), path.join(distDir, "cameras.html"));
copyFile(path.join(srcDir, "images.html"), path.join(distDir, "images.html"));
copyFile(path.join(srcDir, "bags.html"), path.join(distDir, "bags.html"));
copyFile(path.join(srcDir, "recording.html"), path.join(distDir, "recording.html"));
copyFile(path.join(srcDir, "scoring.html"), path.join(distDir, "scoring.html"));
copyFile(path.join(srcDir, "optimization.html"), path.join(distDir, "optimization.html"));
copyFile(path.join(srcDir, "app.js"), path.join(staticDir, "app.js"));
copyFile(path.join(srcDir, "styles.css"), path.join(staticDir, "styles.css"));

console.log("Built web dashboard into " + distDir);
