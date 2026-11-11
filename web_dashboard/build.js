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
]) {
  const file = path.join(dist, target);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.copyFileSync(path.join(root, "src", source), file);
}
console.log("Built phone capture page into " + dist);

const { build } = require("esbuild");
const { parse, compileScript } = require("@vue/compiler-sfc");
build({ entryPoints: [path.join(root, "src/preview/index.js")], bundle: true, minify: true,
  format: "esm", outfile: path.join(dist, "static/preview.js"),
  define: {"process.env.NODE_ENV": '"production"', __VUE_OPTIONS_API__: "false", __VUE_PROD_DEVTOOLS__: "false", __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: "false"},
  plugins: [{ name: "vue", setup(build) { build.onLoad({filter: /\.vue$/}, args => {
    const { descriptor } = parse(fs.readFileSync(args.path, "utf8"), {filename: args.path});
    const result = compileScript(descriptor, {id: "insight-preview", inlineTemplate: true});
    return {contents: result.content, loader: "js", resolveDir: path.dirname(args.path)};
  }); }}],
}).catch(error => {console.error(error); process.exit(1);});
