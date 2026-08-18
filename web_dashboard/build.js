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

copyFile(path.join(srcDir, "3d.html"), path.join(distDir, "3d.html"));
copyFile(path.join(srcDir, "sessions.html"), path.join(distDir, "sessions.html"));
copyFile(path.join(srcDir, "bags.html"), path.join(distDir, "bags.html"));
copyFile(path.join(srcDir, "umi-dataset.html"), path.join(distDir, "umi-dataset.html"));
copyFile(path.join(srcDir, "recording.html"), path.join(distDir, "recording.html"));
copyFile(path.join(srcDir, "scoring.html"), path.join(distDir, "scoring.html"));
copyFile(path.join(srcDir, "handpose.html"), path.join(distDir, "handpose.html"));
copyFile(path.join(srcDir, "optimization.html"), path.join(distDir, "optimization.html"));
copyFile(path.join(srcDir, "settings.html"), path.join(distDir, "settings.html"));
copyTree(path.join(srcDir, "shared"), path.join(staticDir, "shared"));
copyTree(path.join(srcDir, "camera"), path.join(staticDir, "camera"));
copyTree(path.join(srcDir, "spatial"), path.join(staticDir, "spatial"));
copyTree(path.join(srcDir, "handpose"), path.join(staticDir, "handpose"));
copyTree(path.join(srcDir, "pages"), path.join(staticDir, "pages"));
copyFile(path.join(srcDir, "styles.css"), path.join(staticDir, "styles.css"));
copyFile(path.join(srcDir, "fonts", "InterVariable.woff2"), path.join(staticDir, "fonts", "InterVariable.woff2"));

console.log("Built web dashboard into " + distDir);
