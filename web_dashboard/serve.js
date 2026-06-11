const http = require("http");
const fs = require("fs");
const path = require("path");
const url = require("url");

const root = path.join(__dirname, "dist");
const port = Number(process.env.PORT || 8080);

const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8"
};

http.createServer((req, res) => {
  const parsed = url.parse(req.url || "/");
  const cleanPath = decodeURIComponent(parsed.pathname || "/");
  const relativePath = cleanPath === "/" ? "index.html" : cleanPath.replace(/^\/+/, "");
  const filePath = path.join(root, relativePath);
  const resolved = path.resolve(filePath);

  if (!resolved.startsWith(path.resolve(root))) {
    res.statusCode = 403;
    res.end("forbidden");
    return;
  }

  fs.readFile(resolved, (err, data) => {
    if (err) {
      res.statusCode = 404;
      res.end("not found");
      return;
    }
    res.setHeader("Content-Type", types[path.extname(resolved)] || "application/octet-stream");
    res.end(data);
  });
}).listen(port, "0.0.0.0", () => {
  console.log("Serving web dashboard on http://0.0.0.0:" + port);
});
