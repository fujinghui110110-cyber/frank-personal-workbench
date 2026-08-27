const SHELL_CACHE = "frank-personal-workbench-shell-v52";
const SHELL = [
  "/",
  "/static/index.html",
  "/static/app.css?v=52",
  "/static/app.js?v=52",
  "/manifest.webmanifest?v=29",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/static/index.html")));
    return;
  }
  event.respondWith(fetch(request).then((response) => {
    if (response.ok && ["style", "script", "manifest"].includes(request.destination)) {
      const copy = response.clone();
      caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
    }
    return response;
  }).catch(() => caches.match(request)));
});
