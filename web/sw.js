// Driftless Cast service worker.
// Shell: cache-first so the app opens instantly and offline.
// API: network-first, falling back to the last good response — useful in
// coulees with no cell signal. Map tiles are left to the browser cache.
const VERSION = "dc-v1";
const SHELL = ["/", "/index.html", "/styles.css", "/map.js", "/manifest.webmanifest", "/icons/icon.svg"];
const API_PATHS = ["/reaches", "/forecast/", "/best-windows", "/hatch-windows", "/status", "/calendar", "/education", "/residuals", "/scores-grid", "/near"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const isApi = API_PATHS.some((p) => url.pathname === p || url.pathname.startsWith(p));
  if (isApi) {
    e.respondWith(
      fetch(req).then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || Response.error()))
    );
    return;
  }
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req).then((hit) => hit || caches.match("/")))
  );
});
