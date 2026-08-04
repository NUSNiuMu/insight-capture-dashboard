const navigationLinks = Array.from(
  document.querySelectorAll("nav a[href], a.app-brand[href], a.rail-brand[href]")
).filter((link) => {
  const url = new URL(link.href, window.location.href);
  return url.origin === window.location.origin && url.pathname !== window.location.pathname;
});

const destinationUrls = Array.from(new Set(navigationLinks.map((link) => link.href)));
const warmedDocuments = new Set();

for (const link of navigationLinks) {
  const warmDestination = () => prefetchDocument(link.href);
  link.addEventListener("pointerenter", warmDestination, { once: true });
  link.addEventListener("focus", warmDestination, { once: true });
  link.addEventListener("touchstart", warmDestination, { once: true, passive: true });
}

// Page documents are only a few kilobytes. Warm all of them once startup work
// has settled so later switches stay responsive even without hover input.
window.setTimeout(() => scheduleIdle(() => destinationUrls.forEach(prefetchDocument)), 2000);

const spatialAssets = [
  "/static/babylon.js?v=69119e74",
  "/static/babylonjs.loaders.min.js?v=9314a544",
];
let spatialAssetsWarmed = false;

for (const link of navigationLinks) {
  if (new URL(link.href, window.location.href).pathname !== "/3d") continue;
  link.addEventListener("pointerenter", warmSpatialAssets, { once: true });
  link.addEventListener("focus", warmSpatialAssets, { once: true });
  link.addEventListener("touchstart", warmSpatialAssets, { once: true, passive: true });
}

function warmSpatialAssets() {
  if (spatialAssetsWarmed) return;
  spatialAssetsWarmed = true;
  spatialAssets.forEach((href) => {
    const link = document.createElement("link");
    link.rel = "prefetch";
    link.as = "script";
    link.href = href;
    document.head.append(link);
  });
}

function prefetchDocument(href) {
  if (warmedDocuments.has(href)) return;
  warmedDocuments.add(href);
  const link = document.createElement("link");
  link.rel = "prefetch";
  link.as = "document";
  link.href = href;
  document.head.append(link);
}

function scheduleIdle(callback) {
  if ("requestIdleCallback" in window) {
    window.requestIdleCallback(callback, { timeout: 1500 });
  } else {
    window.setTimeout(callback, 250);
  }
}
