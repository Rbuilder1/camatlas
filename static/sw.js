/* CamAtlas service worker — makes the app installable and launchable offline.
 * The app shell (page, styles, map libraries, icons) and the camera lists are cached so the
 * app opens without a network, and map tiles it has shown are kept for offline use. Camera
 * images and live data (/api/*) always go to the network. */
const SHELL = 'camatlas-shell-v3';
const SHELL_FILES = [
  './', './index.html', './app.css', './app.js', './icon.svg',
  './icon-192.png', './icon-512.png', './apple-touch-icon.png', './manifest.webmanifest',
  './vendor/leaflet.min.js', './vendor/leaflet.min.css',
  './vendor/leaflet.markercluster.js', './vendor/MarkerCluster.css',
  './vendor/hls.min.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

// Map tiles fetched straight from Esri (the hosted copy; the app server keeps its own). Kept so the
// map still shows offline: the network is used while there is one, the copy when there isn't.
const TILES = 'camatlas-tiles';
const TILE_HOST = 'server.arcgisonline.com';
const TILE_MAX = 4000;  // tiles kept; the oldest go past this
let tilePuts = 0;

async function trimTiles() {
  const c = await caches.open(TILES);
  const keys = await c.keys();
  await Promise.all(keys.slice(0, Math.max(0, keys.length - TILE_MAX)).map((k) => c.delete(k)));
}

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== TILES).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Live data and media always go to the network (never served stale from cache).
  if (url.origin === self.location.origin && (url.pathname.startsWith('/api/') || url.pathname.startsWith('/snap/'))) return;
  if (url.hostname === TILE_HOST) {
    e.respondWith(fetch(req).then((res) => {
      if (res.ok && res.type === 'cors') {
        const copy = res.clone();
        caches.open(TILES).then((c) => c.put(req.url, copy)).then(() => { if (++tilePuts % 200 === 0) trimTiles(); });
      }
      return res;
    }).catch(() => caches.match(req.url, { cacheName: TILES }).then((hit) => hit || Response.error())));
    return;
  }
  if (url.origin !== self.location.origin) return;  // camera images — let the network handle them
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
