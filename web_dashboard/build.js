const fs = require("fs");
const path = require("path");

const root = __dirname;
const srcDir = path.join(root, "src");
const generatedDir = path.join(root, "generated");
const staticDir = path.join(generatedDir, "static");
const vendorSrcDir = path.join(srcDir, "vendor");
const vendorDistDir = path.join(staticDir, "vendor");

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

resetDir(generatedDir);
resetDir(staticDir);

copyFile(path.join(srcDir, "index.html"), path.join(generatedDir, "index.html"));
copyFile(path.join(srcDir, "3d.html"), path.join(generatedDir, "3d.html"));
copyFile(path.join(srcDir, "cameras.html"), path.join(generatedDir, "cameras.html"));
copyFile(path.join(srcDir, "app.js"), path.join(staticDir, "app.js"));
copyFile(path.join(srcDir, "styles.css"), path.join(staticDir, "styles.css"));
if (fs.existsSync(vendorSrcDir)) {
  fs.mkdirSync(vendorDistDir, { recursive: true });
  for (const file of fs.readdirSync(vendorSrcDir)) {
    copyFile(path.join(vendorSrcDir, file), path.join(vendorDistDir, file));
  }
}

console.log("Built web dashboard into " + generatedDir);
