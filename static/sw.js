/* CamAtlas service worker — makes the app installable and launchable offline.
 * The app shell (page, styles, map libraries, icons) is cached so the app opens
 * without a network; camera data (/api/*, tiles, camera images) is always live. */
const SHELL = 'camatlas-shell-v2';
const SHELL_FILES = [
  './', './index.html', './app.css', './app.js', './icon.svg',
  './icon-192.png', './icon-512.png', './manifest.webmanifest',
  './vendor/leaflet.min.js', './vendor/leaflet.min.css',
  './vendor/leaflet.markercluster.js', './vendor/MarkerCluster.css',
  './vendor/hls.min.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Live data and media always go to the network (never served stale from cache).
  if (url.origin === self.location.origin && (url.pathname.startsWith('/api/') || url.pathname.startsWith('/snap/'))) return;
  if (url.origin !== self.location.origin) return;  // camera images, map tiles — let the network handle them
  // App shell: network first (the server is local, so this is instant) so app updates show
  // up on the next launch; the cache is only the offline fallback. Cache-first here used to
  // pin every install to whatever version it first saw.
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req).then((hit) => hit || caches.match('./index.html')))
  );
});
