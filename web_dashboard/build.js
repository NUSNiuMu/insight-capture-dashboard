const fs = require("fs");
const path = require("path");

const root = __dirname;
const srcDir = path.join(root, "src");
const distDir = path.join(root, "dist");
const staticDir = path.join(distDir, "static");

function resetDir(target) {
  if (fs.existsSync(target)) {
    fs.rmSync(target, { recursive: true });
  }
  fs.mkdirSync(target, { recursive: true });
}

const PRESERVE_STATIC = ["babylon.js", "babylonjs.loaders.min.js"];

function copyFile(source, target) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(source, target);
}

function copyTree(source, target) {
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const sourcePath = path.join(source, entry.name);
    const targetPath = path.join(target, entry.name);
    if (entry.isDirectory()) {
      copyTree(sourcePath, targetPath);
    } else {
      copyFile(sourcePath, targetPath);
    }
  }
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

const pageFiles = [
  ["review/index.html", "3d.html"],
  ["sessions/index.html", "sessions.html"],
  ["storage/index.html", "bags.html"],
  ["datasets/index.html", "umi-dataset.html"],
  ["sessions/recording.html", "recording.html"],
  ["advanced/trajectory/index.html", "scoring.html"],
  ["advanced/handpose/index.html", "handpose.html"],
  ["advanced/optimization/index.html", "optimization.html"],
  ["advanced/system/index.html", "settings.html"],
];
for (const [source, target] of pageFiles) {
  copyFile(path.join(srcDir, source), path.join(distDir, target));
}

const pageScripts = [
  ["review/app.js", "spatial.js"],
  ["sessions/app.js", "sessions.js"],
  ["storage/app.js", "bags.js"],
  ["datasets/app.js", "umi-dataset.js"],
  ["sessions/recording.js", "recording.js"],
  ["advanced/trajectory/app.js", "scoring.js"],
  ["advanced/handpose/app.js", "handpose.js"],
  ["advanced/optimization/app.js", "optimization.js"],
  ["advanced/system/app.js", "settings.js"],
];
for (const [source, target] of pageScripts) {
  copyFile(path.join(srcDir, source), path.join(staticDir, "pages", target));
}
copyTree(path.join(srcDir, "shared"), path.join(staticDir, "shared"));
copyTree(path.join(srcDir, "review", "camera"), path.join(staticDir, "camera"));
copyTree(path.join(srcDir, "review", "spatial"), path.join(staticDir, "spatial"));
copyFile(path.join(srcDir, "advanced", "handpose", "viewer.js"), path.join(staticDir, "handpose", "viewer.js"));
copyFile(path.join(srcDir, "styles.css"), path.join(staticDir, "styles.css"));
copyFile(path.join(srcDir, "fonts", "InterVariable.woff2"), path.join(staticDir, "fonts", "InterVariable.woff2"));

console.log("Built web dashboard into " + distDir);
