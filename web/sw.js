// Caches the app shell only. API calls and the socket always go to the network.
const CACHE = 'landeck-shell-v1';
const SHELL = [
  '/',
  '/manifest.webmanifest',
  '/css/tokens.css', '/css/base.css', '/css/components.css', '/css/views.css',
  '/js/app.js', '/js/api.js', '/js/ws.js', '/js/store.js', '/js/ui.js', '/js/router.js', '/js/icons.js', '/js/charts.js', '/js/clip.js',
  '/js/views/actions.js', '/js/views/clipboard.js', '/js/views/dashboard.js', '/js/views/files.js', '/js/views/media.js',
  '/js/views/more.js', '/js/views/pair.js', '/js/views/power.js', '/js/views/processes.js', '/js/views/remote.js',
  '/js/views/settings.js', '/js/views/terminal.js',
  '/icons/icon-192.png', '/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin') || url.pathname === '/ws' || url.pathname.startsWith('/ca.')) return;
  const isNav = event.request.mode === 'navigate';
  // network first so updates land immediately; the cache is only a fallback for flaky Wi-Fi
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(isNav ? '/' : event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(isNav ? '/' : event.request)),
  );
});
