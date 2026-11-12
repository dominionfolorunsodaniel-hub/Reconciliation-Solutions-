/* RECONZA service worker.
   It only does two small things: it lets the site be installed like an app, and it shows a friendly offline page
   when there is no internet. It NEVER stores API responses, reports, shared links or anything personal. */
const CACHE = "reconza-shell-v4";
const SHELL = ["/offline.html", "/static/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin) return;
  if (u.pathname.startsWith("/api/") || u.pathname.startsWith("/share/")) return;          // never touched
  if (r.mode === "navigate") { e.respondWith(fetch(r).catch(() => caches.match("/offline.html"))); return; }
  if (u.pathname === "/static/icon-192.png") e.respondWith(caches.match(r).then(m => m || fetch(r)));
});
