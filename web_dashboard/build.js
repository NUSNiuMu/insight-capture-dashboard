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

function copyHtml(source, target, version) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const html = fs.readFileSync(source, "utf8")
    .split("/static/styles.css").join(`/static/styles.css?v=${version}`)
    .split("/static/app.js").join(`/static/app.js?v=${version}`);
  fs.writeFileSync(target, html);
}

resetDir(generatedDir);
resetDir(staticDir);

const buildVersion = String(Date.now());

copyHtml(path.join(srcDir, "index.html"), path.join(generatedDir, "index.html"), buildVersion);
copyHtml(path.join(srcDir, "3d.html"), path.join(generatedDir, "3d.html"), buildVersion);
copyHtml(path.join(srcDir, "cameras.html"), path.join(generatedDir, "cameras.html"), buildVersion);
copyFile(path.join(srcDir, "app.js"), path.join(staticDir, "app.js"));
copyFile(path.join(srcDir, "styles.css"), path.join(staticDir, "styles.css"));
if (fs.existsSync(vendorSrcDir)) {
  fs.mkdirSync(vendorDistDir, { recursive: true });
  for (const file of fs.readdirSync(vendorSrcDir)) {
    copyFile(path.join(vendorSrcDir, file), path.join(vendorDistDir, file));
  }
}

console.log("Built web dashboard into " + generatedDir);
