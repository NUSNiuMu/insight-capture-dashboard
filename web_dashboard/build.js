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

function copyFile(source, target) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(source, target);
}

resetDir(distDir);
resetDir(staticDir);

copyFile(path.join(srcDir, "index.html"), path.join(distDir, "index.html"));
copyFile(path.join(srcDir, "3d.html"), path.join(distDir, "3d.html"));
copyFile(path.join(srcDir, "cameras.html"), path.join(distDir, "cameras.html"));
copyFile(path.join(srcDir, "app.js"), path.join(staticDir, "app.js"));
copyFile(path.join(srcDir, "styles.css"), path.join(staticDir, "styles.css"));

console.log("Built web dashboard into " + distDir);
