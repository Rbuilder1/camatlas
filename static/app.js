/* CamAtlas: open public camera map with search and a multi-camera Monitor wall. */
'use strict';

const $ = (id) => document.getElementById(id);
const WALL_MAX = 60;          // tiles on one wall
const WALL_VIDEO_MAX = 9;     // simultaneous video players on a wall
const VIEWER_REFRESH_MS = 8000;

const store = {               // per-viewer UI prefs (best effort)
  get(k, d) { try { const v = localStorage.getItem('camatlas.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('camatlas.' + k, JSON.stringify(v)); } catch {} },
};

const S = {
  sources: [], cams: [], byKey: new Map(),
  off: new Set(store.get('off', [])),
  liveOnly: store.get('liveOnly', false),
  mode: store.get('mode', 'all') === 'cams' ? 'cams' : 'all',   // 'all' = Flock on, 'cams' = Flock off
  favorites: [], walls: [],
  wall: null, drawing: false,
};

// ------------------------------------------------------------------ map
const startView = store.get('view', [39.5, -98.35, 4]);
const map = L.map('map', { zoomControl: false, worldCopyJump: true, minZoom: 2 })
  .setView(startView.slice(0, 2), startView[2]);
// Two basemaps (both keyless Esri): the dark canvas, and satellite imagery with road and
// place labels on top so you can still tell which highway a camera is watching.
const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/';
const tile = (path, opts = {}) => L.tileLayer(ESRI + path + '/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19, ...opts });
const CREDIT = ' · Camera credits under Sources';
const BASEMAPS = {
  dark: [
    tile('Canvas/World_Dark_Gray_Base', { maxNativeZoom: 16,
      attribution: 'Basemap Esri, HERE, Garmin, &copy; OpenStreetMap contributors' + CREDIT }),
    tile('Canvas/World_Dark_Gray_Reference', { maxNativeZoom: 16, pane: 'shadowPane' }),
  ],
  satellite: [
    tile('World_Imagery', { maxNativeZoom: 18,
      attribution: 'Imagery Esri, Maxar, Earthstar Geographics, USDA, USGS, AeroGRID, IGN' + CREDIT }),
    tile('Reference/World_Transportation', { maxNativeZoom: 18, pane: 'shadowPane' }),
    tile('Reference/World_Boundaries_and_Places', { maxNativeZoom: 18, pane: 'shadowPane' }),
  ],
};
let basemap = store.get('basemap', 'dark') === 'satellite' ? 'satellite' : 'dark';
function setBasemap(name) {
  for (const l of BASEMAPS[basemap]) map.removeLayer(l);
  basemap = name;
  for (const l of BASEMAPS[basemap]) l.addTo(map);
  store.set('basemap', basemap);
  const btn = $('basemap');
  btn.classList.toggle('on', basemap === 'satellite');
  btn.title = basemap === 'satellite' ? 'Switch to the dark map' : 'Switch to satellite view';
}
setBasemap(basemap);
$('basemap').onclick = () => setBasemap(basemap === 'satellite' ? 'dark' : 'satellite');
L.control.scale({ imperial: true, position: 'bottomleft' }).addTo(map);
// Guard against the map initializing before its container has a size (e.g. opened hidden).
window.addEventListener('resize', () => map.invalidateSize());
setTimeout(() => map.invalidateSize(), 250);
map.attributionControl.setPrefix(false);
map.on('moveend', () => { const c = map.getCenter(); store.set('view', [+c.lat.toFixed(4), +c.lng.toFixed(4), map.getZoom()]); });

const cluster = L.markerClusterGroup({
  chunkedLoading: true, showCoverageOnHover: false, maxClusterRadius: 48, spiderfyOnMaxZoom: true,
  iconCreateFunction(c) {
    const kids = c.getAllChildMarkers();
    const n = kids.length;
    const live = kids.some((m) => m.cam.video);
    const size = n < 10 ? 30 : n < 100 ? 36 : n < 1000 ? 42 : 48;
    const label = n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : n;
    return L.divIcon({ html: `<div class="clu${live ? ' live' : ''}" style="width:${size}px;height:${size}px">${label}</div>`,
      className: '', iconSize: [size, size] });
  },
});
map.addLayer(cluster);

const pinIcons = {};
function pinIcon(cam) {
  const k = cam.alpr ? 'alpr' : (cam.video ? 'live' : '') + (isFav(cam) ? ' fav' : '');
  return pinIcons[k] || (pinIcons[k] = L.divIcon({ html: `<div class="pin ${k}"></div>`, className: '', iconSize: [12, 12] }));
}

// Flock / ALPR surveillance-camera locations — a lazy, viewport-rendered layer (no video feeds).
// 116k points freeze a normal marker layer, so we draw only those in view, downsampled to a cap.
const ALPR_ICON = L.divIcon({ html: '<div class="pin alpr"></div>', className: '', iconSize: [12, 12] });
const FLOCK_CAP = 4000;  // most markers drawn at once
const flockLayer = L.layerGroup();
let flockPts = null, flockLoaded = false, flockLoading = false;
const flockShown = () => S.mode !== 'cams' && !S.off.has('flock');

// Keyless Street View embed: Google snaps to the nearest panorama, or shows a blank pane if there's none.
const svEmbed = (lat, lon) => `https://www.google.com/maps?layer=c&cbll=${lat},${lon}&cbp=11,0,0,0,0&output=svembed`;
const svLink = (lat, lon) => `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lon}`;

function flockPopup(lat, lon, ref) {
  const osm = `https://www.openstreetmap.org/#map=19/${lat}/${lon}`;
  const el = document.createElement('div');
  el.innerHTML = `<b>Flock ALPR camera</b>${ref ? ' · <span class="ref"></span>' : ''}<br>`
    + `<iframe class="sv" src="${svEmbed(lat, lon)}" loading="lazy" allowfullscreen referrerpolicy="no-referrer-when-downgrade"></iframe>`
    + `<button class="btn sv-big">⛶ Big screen</button>`
    + `<span class="muted">Automated license-plate reader. No public video — this is a reported location only.</span><br>`
    + `<span class="muted">${lat.toFixed(5)}, ${lon.toFixed(5)} · source: OpenStreetMap / DeFlock</span><br>`
    + `<a href="${svLink(lat, lon)}" target="_blank" rel="noopener">Open in Street View</a> · `
    + `<a href="${osm}" target="_blank" rel="noopener">View on OpenStreetMap</a>`;
  if (ref) el.querySelector('.ref').textContent = ref;
  el.querySelector('.sv-big').onclick = () => openStreetBig(lat, lon, ref);
  return el;
}

function openStreetBig(lat, lon, ref) {
  map.closePopup();  // one Street View at a time
  $('svTitle').textContent = 'Flock ALPR camera' + (ref ? ' · ' + ref : '');
  $('svMeta').textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  $('svOpen').href = svLink(lat, lon);
  $('svFrame').src = svEmbed(lat, lon);
  show('svBig');
}
function closeStreetBig() {
  if (document.fullscreenElement) document.exitFullscreen();
  $('svFrame').src = 'about:blank';
  hide('svBig');
}
$('svClose').onclick = closeStreetBig;
$('svFull').onclick = () => document.fullscreenElement ? document.exitFullscreen() : $('svBig').requestFullscreen();

function renderFlock() {
  if (!flockLoaded || !flockShown()) { flockLayer.clearLayers(); updateStats(); return; }
  const b = map.getBounds().pad(0.25);
  const inView = [];
  for (const p of flockPts) if (b.contains(p)) inView.push(p);  // p = [lat, lon, ref]
  const stride = Math.max(1, Math.ceil(inView.length / FLOCK_CAP));  // thin out when there are too many
  flockLayer.clearLayers();
  for (let i = 0; i < inView.length; i += stride) {
    const [lat, lon, ref] = inView[i];
    L.marker([lat, lon], { icon: ALPR_ICON })
      .bindPopup(() => flockPopup(lat, lon, ref), { className: 'alpr-popup', maxWidth: 380, minWidth: 340 })
      .addTo(flockLayer);
  }
  flockLayer._inView = inView.length;
  flockLayer._shown = Math.ceil(inView.length / stride);
  updateStats();
}
const renderFlockSoon = (() => { let t; return () => { clearTimeout(t); t = setTimeout(renderFlock, 150); }; })();
map.on('moveend zoomend resize', () => { if (flockLoaded && flockShown()) renderFlockSoon(); });

async function loadFlock() {
  if (flockLoaded || flockLoading) return;
  flockLoading = true;
  try {
    const data = await api('/api/flock');
    if ((!data.pts || !data.pts.length) && (data.loading || !data.error)) {
      flockLoading = false;
      toast('Loading Flock camera locations…');
      return setTimeout(loadFlock, 4000);
    }
    flockPts = data.pts || [];
    flockLoaded = true;
    map.addLayer(flockLayer);
    renderFlock();
  } catch (e) {
    toast('Could not load Flock locations');
  }
  flockLoading = false;
}

function setFlock(on) {
  if (on) { map.addLayer(flockLayer); flockLoaded ? renderFlock() : loadFlock(); }
  else { map.removeLayer(flockLayer); updateStats(); }
}

// ------------------------------------------------------------------ gas stations
// OpenStreetMap's fuel stations (amenity=fuel) from the public Overpass API, fetched for the area in
// view once zoomed in to town level; there are far too many to load for a whole country. Straight from
// the page, so it works the same in the app, on the hosted copy and on the PC. No prices: OSM has none.
const GAS_MIN_ZOOM = 11;
const OVERPASS = ['https://overpass-api.de/api/interpreter', 'https://maps.mail.ru/osm/tools/overpass/api/interpreter'];
const GAS_TILE = 0.25;             // degrees; stations are fetched and remembered per tile
const GAS_KEEP = 7 * 864e5;        // remembered tiles are reused for a week
const gasTileKey = (i, j) => `gas.${i}.${j}`;
const GAS_ICON = L.divIcon({ html: '<div class="pin gas"><svg viewBox="0 0 24 24"><path d="M4 21V5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v16M3 21h13M7 8h5M15 10h2a2 2 0 0 1 2 2v5a1.5 1.5 0 0 0 3 0V9l-3-3"/></svg></div>',
  className: '', iconSize: [22, 22], iconAnchor: [11, 11] });
const gasLayer = L.layerGroup();
const gasSeen = new Map();     // OSM id -> marker, so panning back never draws a station twice
let gasOn = store.get('gas', false), gasBusy = false;
const gasTilesDone = new Set();   // tiles drawn this session

function gasPopup(t, lat, lon) {
  const el = document.createElement('div');
  const name = t.name || t.brand || 'Gas station';
  const addr = [[t['addr:housenumber'], t['addr:street']].filter(Boolean).join(' '), t['addr:city']].filter(Boolean).join(', ');
  const fuels = Object.keys(t).filter((k) => k.startsWith('fuel:') && t[k] === 'yes')
    .map((k) => k.slice(5).replace(/_/g, ' ').replace(/^octane /, '') ).join(', ');
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
  const dir = ios ? `https://maps.apple.com/?daddr=${lat},${lon}` : `https://www.google.com/maps/dir/?api=1&destination=${lat},${lon}`;
  el.innerHTML = '<b class="gname"></b><div class="muted gmeta"></div><div class="gfuel"></div>'
    + `<a class="btn gdir" href="${dir}" target="_blank" rel="noopener">Directions</a>`
    + '<div class="muted small">© OpenStreetMap contributors</div>';
  el.querySelector('.gname').textContent = name + (t.brand && t.name && !t.name.includes(t.brand) ? ` (${t.brand})` : '');
  el.querySelector('.gmeta').textContent = [addr, t.opening_hours === '24/7' ? 'Open 24 hours' : t.opening_hours].filter(Boolean).join(' · ');
  el.querySelector('.gfuel').textContent = fuels ? 'Fuel: ' + fuels : '';
  return el;
}

// Only the tags the popup uses are kept, so a week of browsing fits easily in the phone's storage.
const GAS_TAGS = /^(name|brand|opening_hours|addr:(housenumber|street|city)|fuel:.*)$/;

function drawGas(stations) {
  for (const [key, lat, lon, t] of stations) {
    if (gasSeen.has(key)) continue;
    gasSeen.set(key, L.marker([lat, lon], { icon: GAS_ICON, title: t.name || t.brand || 'Gas station', zIndexOffset: -100 })
      .bindPopup(() => gasPopup(t, lat, lon), { className: 'gas-popup', maxWidth: 280 })
      .addTo(gasLayer));
  }
}

// Public Overpass servers are often at capacity, so the same query goes to two at once and the first
// good answer wins; the other request is cancelled.
async function overpass(q) {
  const ctrls = OVERPASS.map(() => new AbortController());
  const timer = setTimeout(() => ctrls.forEach((c) => c.abort()), 60000);
  try {
    return await Promise.any(OVERPASS.map(async (url, i) => {
      const r = await fetch(url, { method: 'POST', body: 'data=' + encodeURIComponent(q), signal: ctrls[i].signal,
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
      if (!r.ok) throw new Error(url + ' ' + r.status);
      const data = await r.json();
      ctrls.forEach((c, k) => k !== i && c.abort());
      return data;
    }));
  } finally {
    clearTimeout(timer);
  }
}

async function loadGas() {
  if (!gasOn || gasBusy) return;
  if (map.getZoom() < GAS_MIN_ZOOM) { updateStats(); return; }
  const b = map.getBounds().pad(0.2);
  const tiles = [];
  for (let i = Math.floor(b.getSouth() / GAS_TILE); i <= Math.floor(b.getNorth() / GAS_TILE); i++)
    for (let j = Math.floor(b.getWest() / GAS_TILE); j <= Math.floor(b.getEast() / GAS_TILE); j++) tiles.push([i, j]);
  const missing = [];
  for (const [i, j] of tiles) {  // tiles remembered from earlier (this week) draw straight away
    const k = gasTileKey(i, j);
    if (gasTilesDone.has(k)) continue;
    const saved = store.get(k, null);
    if (saved && Date.now() - saved.t < GAS_KEEP) { drawGas(saved.s); gasTilesDone.add(k); }
    else missing.push([i, j]);
  }
  updateStats();
  if (!missing.length) return;
  const s = Math.min(...missing.map((m) => m[0])) * GAS_TILE, n = (Math.max(...missing.map((m) => m[0])) + 1) * GAS_TILE;
  const w = Math.min(...missing.map((m) => m[1])) * GAS_TILE, e = (Math.max(...missing.map((m) => m[1])) + 1) * GAS_TILE;
  const box = [s, w, n, e].map((v) => v.toFixed(4)).join(',');
  const q = `[out:json][timeout:50];(node["amenity"="fuel"](${box});way["amenity"="fuel"](${box}););out center tags 3000;`;
  gasBusy = true;
  const slow = setTimeout(() => toast('Loading gas stations…'), 1500);
  try {
    const data = await overpass(q);
    const byTile = new Map(missing.map(([i, j]) => [gasTileKey(i, j), []]));
    for (const el of data.elements || []) {
      const lat = el.lat ?? (el.center && el.center.lat), lon = el.lon ?? (el.center && el.center.lon);
      if (lat == null) continue;
      const tags = Object.fromEntries(Object.entries(el.tags || {}).filter(([k]) => GAS_TAGS.test(k)));
      const list = byTile.get(gasTileKey(Math.floor(lat / GAS_TILE), Math.floor(lon / GAS_TILE)));
      if (list) list.push([el.type + el.id, +lat.toFixed(6), +lon.toFixed(6), tags]);
    }
    for (const [k, list] of byTile) { store.set(k, { t: Date.now(), s: list }); drawGas(list); gasTilesDone.add(k); }
    loadGasSoon();  // picks up any area the map moved to while this was loading
  } catch {
    toast('Gas station servers are busy right now; try again in a minute');
  }
  clearTimeout(slow);
  gasBusy = false;
  updateStats();
}
const loadGasSoon = (() => { let t; return () => { clearTimeout(t); t = setTimeout(loadGas, 600); }; })();
map.on('moveend', () => { if (gasOn) loadGasSoon(); });

function setGas(on) {
  gasOn = on;
  store.set('gas', on);
  $('gasBtn').classList.toggle('on', on);
  if (on) {
    map.addLayer(gasLayer);
    if (map.getZoom() < GAS_MIN_ZOOM) toast('Zoom in to a town to see gas stations');
    loadGas();
  } else {
    map.removeLayer(gasLayer);
    updateStats();
  }
}
$('gasBtn').onclick = () => setGas(!gasOn);

$('zin').onclick = () => map.zoomIn();
$('zout').onclick = () => map.zoomOut();

// ------------------------------------------------------------------ "find me" GPS mode
// Tap once: locate, drop a live "you are here" dot (with an accuracy ring) and center on it.
// It keeps tracking as you move. Tap again: if you've panned away, recenter on the dot;
// if you're already there, stop tracking and remove the dot.
const geo = { watchId: null, marker: null, accuracy: null, last: null };
const GEO_ICON = L.divIcon({
  html: '<div class="me-dot-wrap"><span class="ping"></span><span class="dot"></span></div>',
  className: '', iconSize: [20, 20],
});

function geoPlace(lat, lon, accuracyM) {
  const ll = [lat, lon];
  geo.last = ll;
  if (!geo.marker) {
    geo.marker = L.marker(ll, { icon: GEO_ICON, zIndexOffset: 1000, interactive: false, keyboard: false }).addTo(map);
    geo.accuracy = L.circle(ll, { radius: accuracyM, className: 'me-accuracy', weight: 1.5, interactive: false }).addTo(map);
  } else {
    geo.marker.setLatLng(ll);
    geo.accuracy.setLatLng(ll);
    geo.accuracy.setRadius(accuracyM);
  }
}

function geoStop() {
  if (geo.watchId != null) navigator.geolocation.clearWatch(geo.watchId);
  geo.watchId = null;
  if (geo.marker) { map.removeLayer(geo.marker); geo.marker = null; }
  if (geo.accuracy) { map.removeLayer(geo.accuracy); geo.accuracy = null; }
  geo.last = null;
  $('locate').classList.remove('on', 'locating');
}

function geoStart() {
  if (!navigator.geolocation) return toast('Location is not available');
  $('locate').classList.add('locating');
  let first = true;
  geo.watchId = navigator.geolocation.watchPosition(
    (p) => {
      const { latitude: lat, longitude: lon, accuracy } = p.coords;
      geoPlace(lat, lon, accuracy);
      $('locate').classList.remove('locating');
      $('locate').classList.add('on');
      if (first) { first = false; map.flyTo([lat, lon], Math.max(map.getZoom(), 14), { duration: .8 }); }
    },
    (err) => {
      geoStop();
      const messages = {
        [err.PERMISSION_DENIED]: 'Location permission is blocked for this app — allow it in your browser\'s site settings',
        [err.POSITION_UNAVAILABLE]: 'Windows couldn\'t determine a location — check that Location Services is on (Settings → Privacy → Location) and Wi-Fi is enabled',
        [err.TIMEOUT]: 'Location lookup timed out — check your connection and try again',
      };
      toast(messages[err.code] || `Could not get your location (${err.message || 'unknown error'})`, 5500);
    },
    { enableHighAccuracy: true, maximumAge: 4000, timeout: 20000 },
  );
}

$('locate').onclick = () => {
  if (geo.watchId == null) return geoStart();
  if (geo.last && map.getCenter().distanceTo(geo.last) > 60) {
    map.flyTo(geo.last, Math.max(map.getZoom(), 14), { duration: .6 });  // panned away: recenter instead of stopping
  } else {
    geoStop();
  }
};

// ------------------------------------------------------------------ data
// The hosted copy (GitHub Pages, for iPhones) has no server: web/build_static.py writes the camera
// lists as files and marks the page with window.CAMATLAS_STATIC. There, each /api/ call is answered
// here instead: lists from those files, favorites in this browser, place search straight from OSM.
const STATIC = !!window.CAMATLAS_STATIC;
const staticApi = {
  'cameras': () => fetch('data/cameras.json').then((r) => r.json()),
  'flock': () => fetch('data/flock.json').then((r) => r.json()),
  'status': () => ({ loading: [] }),
  'app': () => ({ share: true, static: true }),  // shares the site's link (shareSite)
  'state': (opts) => {
    if (opts && opts.method === 'POST') { store.set('state', JSON.parse(opts.body)); return { ok: true }; }
    return store.get('state', { favorites: [], walls: [] });
  },
  'geocode': async (_, q) => {
    const rows = await fetch('https://nominatim.openstreetmap.org/search?' + new URLSearchParams(
      { q: q.get('q'), format: 'jsonv2', limit: 6 }), { headers: { 'Accept-Language': 'en' } }).then((r) => r.json());
    return rows.map((r) => ({ name: r.display_name, lat: +r.lat, lon: +r.lon, bbox: (r.boundingbox || []).map(Number) }));
  },
};
const api = async (path, opts) => {
  if (STATIC) {
    const u = new URL(path, location.origin);
    const fn = staticApi[u.pathname.replace('/api/', '')];
    if (!fn) throw new Error(path + ' not available here');
    return fn(opts, u.searchParams);
  }
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(path + ' ' + r.status);
  return r.json();
};

async function loadCameras() {
  const data = await api('/api/cameras');
  S.sources = data.sources;
  S.cams = [];
  S.byKey.clear();
  for (const src of data.sources) {
    for (const [id, name, lat, lon, img, video, dir, region] of data.cameras[src.id] || []) {
      const cam = { key: src.id + ':' + id, src: src.id, id, name, lat, lon, img, video, dir, region,
        alpr: src.id === 'flock', kind: !video ? 'still' : /\.m3u8/i.test(video) ? 'live' : 'clip',
        lname: (name + ' ' + region).toLowerCase() };
      S.cams.push(cam);
      S.byKey.set(cam.key, cam);
    }
  }
  renderSources();
  rebuildMarkers();
  if (flockShown() && !flockLoaded) loadFlock();  // Flock layer is lazy; load it if shown
  if (S.sources.some((s) => s.loading)) setTimeout(waitForLoading, 3000);
}

async function waitForLoading() {
  const st = await api('/api/status').catch(() => ({ loading: [] }));
  if (st.loading.length) { updateStats(st.loading); setTimeout(waitForLoading, 3000); }
  else loadCameras();
}

const visible = (cam) => S.mode !== 'flock' && !S.off.has(cam.src) && (!S.liveOnly || cam.video);

function rebuildMarkers() {
  cluster.clearLayers();
  const markers = [];
  for (const cam of S.cams) {
    if (!visible(cam)) continue;
    const m = L.marker([cam.lat, cam.lon], { icon: pinIcon(cam), title: cam.name });
    m.cam = cam;
    m.on('click', () => openViewer(cam));
    markers.push(m);
  }
  cluster.addLayers(markers);
  updateStats();
}

function updateStats(loading = S.sources.filter((s) => s.loading).map((s) => s.id)) {
  const shown = S.cams.filter(visible).length;
  const live = S.cams.filter((c) => visible(c) && c.video).length;
  const srcs = S.sources.filter((s) => s.count && !s.lazy && !S.off.has(s.id)).length;
  const flockTotal = flockLoaded && flockShown() ? flockPts.length : 0;
  const flockView = flockLayer._inView || 0;
  $('stats').textContent = [
    `${shown.toLocaleString()} cameras · ${live.toLocaleString()} with video · ${srcs} sources`,
    flockTotal && `${flockTotal.toLocaleString()} Flock ALPRs (${flockView.toLocaleString()} in view)`,
    gasOn && (map.getZoom() < GAS_MIN_ZOOM ? 'zoom in for gas stations' : `${gasSeen.size.toLocaleString()} gas stations`),
    loading.length && `updating ${loading.join(', ')}…`,
  ].filter(Boolean).join(' · ');
}

// ------------------------------------------------------------------ state (favorites, walls)
async function loadState() {
  const st = await api('/api/state').catch(() => ({ favorites: [], walls: [] }));
  S.favorites = st.favorites || [];
  S.walls = st.walls || [];
  renderFavs();
}
const saveState = () => api('/api/state', { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ favorites: S.favorites, walls: S.walls }) }).catch(() => toast('Could not save'));
const isFav = (cam) => S.favorites.includes(cam.key);

function toggleFav(cam) {
  if (isFav(cam)) S.favorites = S.favorites.filter((k) => k !== cam.key);
  else S.favorites.unshift(cam.key);
  saveState();
  renderFavs();
  rebuildMarkers();
}

function renderFavs() {
  $('favCount').textContent = S.favorites.length || '';
  const fl = $('favList');
  fl.innerHTML = '';
  const favs = S.favorites.map((k) => S.byKey.get(k)).filter(Boolean);
  if (!favs.length) fl.innerHTML = '<div class="empty">Star a camera to keep it here.</div>';
  for (const cam of favs) fl.append(listItem(cam, () => openViewer(cam, true), () => toggleFav(cam)));
  $('favWall').hidden = favs.length < 2;
  const wl = $('wallList');
  wl.innerHTML = '';
  if (!S.walls.length) wl.innerHTML = '<div class="empty">Drag a box with Monitor, then “Save wall”.</div>';
  S.walls.forEach((w, i) => {
    const el = document.createElement('div');
    el.className = 'li';
    el.innerHTML = `<div class="t"><div></div><small>${w.keys.length} cameras</small></div><button class="del" title="Delete">&times;</button>`;
    el.querySelector('.t div').textContent = w.name;
    el.onclick = () => { hide('favs'); openWall(w.keys.map((k) => S.byKey.get(k)).filter(Boolean), w.name); };
    el.querySelector('.del').onclick = (e) => { e.stopPropagation(); S.walls.splice(i, 1); saveState(); renderFavs(); };
    wl.append(el);
  });
}

function listItem(cam, onClick, onDel) {
  const el = document.createElement('div');
  el.className = 'li';
  el.innerHTML = `<img loading="lazy" alt=""><div class="t"><div></div><small></small></div>` + (onDel ? '<button class="del" title="Remove">&times;</button>' : '');
  el.querySelector('img').src = thumb(cam);
  el.querySelector('.t div').textContent = cam.name;
  el.querySelector('small').textContent = cam.region;
  el.onclick = onClick;
  if (onDel) el.querySelector('.del').onclick = (e) => { e.stopPropagation(); onDel(); };
  return el;
}

$('favWall').onclick = () => { hide('favs'); openWall(S.favorites.map((k) => S.byKey.get(k)).filter(Boolean), 'Favorites'); };

// ------------------------------------------------------------------ sources panel
function renderSources() {
  const box = $('sourceList');
  box.innerHTML = '';
  for (const s of S.sources) {
    const el = document.createElement('label');
    el.className = 'src';
    const when = s.updated ? 'updated ' + ago(s.updated * 1000) : '';
    const sub = s.loading ? 'downloading…' : s.error && !s.count ? s.error : s.error ? `kept last list · ${s.error}` : `${s.attribution} · ${when}` + (s.hidden ? ` · ${s.hidden} offline hidden` : '');
    el.innerHTML = `<input type="checkbox"><span></span><span class="n">${s.count.toLocaleString()}</span><span class="sub"></span>`;
    el.querySelector('input').checked = !S.off.has(s.id);
    el.querySelector('span').textContent = s.name;
    const subEl = el.querySelector('.sub');
    subEl.textContent = sub;
    if (s.error) subEl.classList.add('err');
    el.querySelector('input').onchange = (e) => {
      e.target.checked ? S.off.delete(s.id) : S.off.add(s.id);
      store.set('off', [...S.off]);
      if (s.lazy) setFlock(e.target.checked);
      else rebuildMarkers();
    };
    box.append(el);
  }
}

$('refreshBtn').onclick = async () => {
  await api('/api/refresh', { method: 'POST' });
  toast('Re-downloading camera lists…');
  setTimeout(waitForLoading, 1500);
};

// ------------------------------------------------------------------ find more cameras
// The server searches open-data catalogs and tests each dataset; found feeds are listed
// here to add. Adding one makes it an ordinary source, so the map picks it up on reload.
let discPolling = false;

function renderDiscovery(st) {
  $('discoverBtn').disabled = st.running;
  $('discoverBtn').textContent = st.running ? `Searching… ${st.checked}/${st.total || '?'}` : 'Find more cameras';
  $('discStatus').textContent = st.status || 'Searches public open-data catalogs for camera feeds that aren\'t on the map yet.';
  const box = $('discList');
  box.innerHTML = '';
  const pending = st.found.filter((d) => !d.added);
  if (pending.length > 1) {
    const all = document.createElement('button');
    all.className = 'btn ghost';
    all.textContent = `Add all ${pending.length}`;
    all.onclick = async () => { for (const d of pending) await setDiscovered(d, true, false); afterDiscoveredChange(); };
    box.append(all);
  }
  for (const d of st.found) {
    const el = document.createElement('div');
    el.className = 'disc';
    el.innerHTML = '<img alt="" loading="lazy"><div><b></b><span class="sub muted"></span></div><button class="btn ghost"></button>';
    el.querySelector('img').src = d.preview || 'icon-192.png';
    el.querySelector('img').onerror = (e) => { e.target.src = 'icon-192.png'; };
    el.querySelector('b').textContent = d.title;
    el.querySelector('.sub').textContent = `${d.count.toLocaleString()} cameras · ${d.owner}`;
    const b = el.querySelector('button');
    b.textContent = d.added ? 'Remove' : 'Add';
    b.onclick = () => setDiscovered(d, !d.added);
    box.append(el);
  }
}

async function pollDiscovery() {
  const st = await api('/api/discover').catch(() => null);
  if (!st) return;
  renderDiscovery(st);
  if (st.running) setTimeout(pollDiscovery, 2000);
  else if (discPolling) { discPolling = false; toast(st.status); }
}

async function setDiscovered(d, add, reload = true) {
  await api('/api/discover/' + (add ? 'add' : 'remove'), { method: 'POST', body: JSON.stringify({ id: d.id }) });
  if (reload) afterDiscoveredChange(add ? `Adding ${d.title}…` : `Removed ${d.title}`);
}

function afterDiscoveredChange(msg = 'Adding cameras…') {
  toast(msg);
  pollDiscovery();
  setTimeout(waitForLoading, 1000);
}

$('discoverBtn').onclick = async () => {
  await api('/api/discover', { method: 'POST' });
  discPolling = true;
  setTimeout(pollDiscovery, 500);
};

// ------------------------------------------------------------------ share the app (Android)
api('/api/app').then((a) => {
  $('shareRow').hidden = !a.share;
  if (a.version) {  // the Android app: which version, and which page/code build it's running
    $('appVersion').textContent = `CamWorld ${a.version} · build ${a.content || 'built-in'}`
      + (a.updated ? ' (updated over the air)' : '') + ' · updates automatically';
    $('appVersion').hidden = false;
  }
}).catch(() => {});
const shareApp = (how) => async () => {
  if (STATIC) return how === 'text' ? textSite() : shareSite();
  try { await api('/api/share-app', { method: 'POST', body: JSON.stringify({ how }) }); }
  catch { toast('Couldn\'t open sharing'); }
};

// The hosted copy shares its own address: as a new text message, or through the phone's share sheet.
// Searching and re-downloading need the app's own server, so those controls go.
const SITE_TEXT = 'Check out CamWorld, a map of live public traffic and weather cameras.\n'
  + 'iPhone: https://rbuilder1.github.io/camatlas/ (open in Safari, tap Share, then Add to Home Screen)\n'
  + 'Android: https://github.com/Rbuilder1/camatlas/releases/latest/download/CamAtlas.apk';
function textSite() {
  // A new message in Messages with the text filled in; iPhones want "sms:&body=", Android "sms:?body=".
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
  location.href = 'sms:' + (ios ? '&' : '?') + 'body=' + encodeURIComponent(SITE_TEXT);
}
async function shareSite() {
  try {
    if (navigator.share) await navigator.share({ title: 'CamWorld', text: SITE_TEXT });
    else { await navigator.clipboard.writeText(SITE_TEXT); toast('Link copied'); }
  } catch (e) { if (e.name !== 'AbortError') toast('Couldn\'t open sharing'); }
}
if (STATIC) {
  $('shareFileBtn').remove();
  $('refreshBtn').parentElement.remove();
  $('discoverBtn').parentElement.remove();
  showInstallGuide();
}

// iPhones can't install apps from a link, but a home-screen web app is the next best thing; few people
// know the steps, so the hosted page walks iPhone visitors through them. Not shown once it's running
// from the home screen, and "Not now" keeps it away for a week.
function showInstallGuide() {
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);  // iPadOS reports a Mac
  const installed = navigator.standalone || matchMedia('(display-mode: standalone)').matches;
  if (!ios || installed || Date.now() - store.get('installLater', 0) < 7 * 864e5) return;
  if (/CriOS|FxiOS|EdgiOS/.test(navigator.userAgent)) {  // Chrome / Firefox / Edge on iPhone
    $('installStep1').innerHTML = 'Tap <b>Share</b> <svg viewBox="0 0 24 24" class="ios-share"><path d="M12 3v12M8 7l4-4 4 4M5 11v9h14v-9"/></svg> '
      + 'in the address bar (or open this page in <b>Safari</b>)';
  }
  setTimeout(() => { $('installSheet').hidden = false; }, 1500);
  $('installClose').onclick = () => { $('installSheet').hidden = true; store.set('installLater', Date.now()); };
}
$('shareTextBtn').onclick = shareApp('text');
$('shareLinkBtn').onclick = shareApp('link');
if ($('shareFileBtn')) $('shareFileBtn').onclick = shareApp('file');

// ------------------------------------------------------------------ panels
const show = (id) => { $(id).hidden = false; };
const hide = (id) => {
  $(id).hidden = true;
  if (id === 'viewer') stopMedia($('vMedia'));
};
document.querySelectorAll('[data-close]').forEach((b) => { b.onclick = () => hide(b.dataset.close); });
$('layersBtn').onclick = () => { if ($('layers').hidden) { show('layers'); pollDiscovery(); } else hide('layers'); };
$('favBtn').onclick = () => { if ($('favs').hidden) { hide('viewer'); show('favs'); } else hide('favs'); };
$('liveBtn').onclick = () => setLiveOnly(!S.liveOnly);
function setLiveOnly(on) {
  S.liveOnly = on;
  store.set('liveOnly', on);
  $('liveBtn').classList.toggle('on', on);
  rebuildMarkers();
}
$('liveBtn').classList.toggle('on', S.liveOnly);

// Flock on/off switch: ON shows the Flock layer (mode 'all'), OFF hides it (mode 'cams').
function setFlockOn(on) {
  S.mode = on ? 'all' : 'cams';
  store.set('mode', S.mode);
  $('flockSw').setAttribute('aria-checked', on ? 'true' : 'false');
  map.closePopup();
  setFlock(flockShown());
  updateStats();
}
$('flockSw').onclick = () => setFlockOn(!flockShown());
$('flockSw').setAttribute('aria-checked', flockShown() ? 'true' : 'false');

let toastTimer;
function toast(msg, ms = 2600) {
  $('toast').textContent = msg;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, ms);
}

function ago(t) {
  const s = Math.max(0, (Date.now() - t) / 1000);
  return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
}

// ------------------------------------------------------------------ media helpers
const bust = (url) => url + (url.includes('?') ? '&' : '?') + '_=' + Math.floor(Date.now() / 1000);
// Some agencies (MD, DE, MO) publish live video but no still image; those get this card wherever
// a thumbnail would go.
const LIVE_THUMB = 'data:image/svg+xml,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 100"><rect width="160" height="100" fill="#14161b"/>' +
  '<circle cx="62" cy="50" r="5" fill="#ef4444"/><text x="74" y="56" font-family="system-ui,sans-serif" font-size="17" ' +
  'font-weight="700" fill="#e5e7eb">LIVE</text><text x="80" y="80" text-anchor="middle" font-family="system-ui,sans-serif" ' +
  'font-size="10" fill="#8b909a">video only</text></svg>');
const thumb = (cam) => cam.img || LIVE_THUMB;
// Tell the server a camera's picture or stream just failed, so it rechecks (and hides it if it's dead).
const reported = new Set();
function report(cam) {
  if (STATIC || !cam || !cam.src || reported.has(cam.key)) return;
  reported.add(cam.key);
  fetch(`/api/report?src=${encodeURIComponent(cam.src)}&id=${encodeURIComponent(cam.id)}`).catch(() => {});
}

/** Puts a camera into a container: live HLS, a looping clip, or a still that refreshes. Returns a stop function. */
function mountMedia(box, cam, { video = true, refreshMs = VIEWER_REFRESH_MS, onFrame } = {}) {
  stopMedia(box);
  box.querySelectorAll('img,video,.badge,.msg').forEach((n) => n.remove());
  const timers = [];
  let hls = null;
  if (video && cam.video) {
    const v = document.createElement('video');
    Object.assign(v, { muted: true, autoplay: true, playsInline: true, loop: cam.kind === 'clip', poster: cam.img });
    v.controls = box.classList.contains('media');
    if (cam.kind === 'live' && window.Hls && Hls.isSupported()) {
      hls = new Hls({ liveDurationInfinity: true, maxBufferLength: 10 });
      hls.loadSource(cam.video);
      hls.attachMedia(v);
      hls.on(Hls.Events.ERROR, (_, d) => { if (d.fatal) { hls.destroy(); hls = null; report(cam); swapToStill(); } });
    } else {
      v.src = cam.video;
      v.onerror = () => { report(cam); swapToStill(); };
      if (cam.kind === 'clip') timers.push(setInterval(() => { v.src = bust(cam.video); }, 60000)); // TfL refreshes clips ~every few min
    }
    box.prepend(v);
    badge(cam.kind === 'live' ? 'LIVE' : 'CLIP', cam.kind);
  } else {
    swapToStill(true);
  }
  function badge(text, kind) {
    const b = document.createElement('span');
    b.className = 'badge' + (kind === 'clip' ? ' clip' : '');
    b.textContent = text;
    box.append(b);
  }
  function swapToStill(first) {
    box.querySelectorAll('video,.badge').forEach((n) => n.remove());
    const img = document.createElement('img');
    img.alt = cam.name;
    if (!cam.img) {  // video-only camera that isn't playing here: show the card, nothing to refresh
      img.src = LIVE_THUMB;
      box.prepend(img);
      return;
    }
    img.decoding = 'async';
    img.src = first ? cam.img : bust(cam.img);
    img.onload = () => { box.classList.remove('dead'); onFrame && onFrame(); };
    img.onerror = () => { box.classList.add('dead'); report(cam); };
    box.prepend(img);
    timers.push(setInterval(() => {
      const next = new Image();             // preload, then swap, so tiles never flash black
      next.onload = () => { img.src = next.src; box.classList.remove('dead'); onFrame && onFrame(); };
      next.src = bust(cam.img);
    }, refreshMs + Math.random() * 1500));
  }
  box._stop = () => {
    timers.forEach(clearInterval);
    if (hls) hls.destroy();
    box.querySelectorAll('video').forEach((v) => { v.pause(); v.removeAttribute('src'); v.load(); });
  };
}
function stopMedia(box) { if (box._stop) { box._stop(); box._stop = null; } }

// ------------------------------------------------------------------ single camera viewer
let current = null;
function openViewer(cam, fly) {
  current = cam;
  hide('favs');
  show('viewer');
  $('vName').textContent = cam.name;
  const src = S.sources.find((s) => s.id === cam.src);
  $('vMeta').textContent = [cam.region, cam.dir && 'facing ' + cam.dir, src && src.attribution].filter(Boolean).join(' · ');
  $('vAge').textContent = cam.video ? '' : 'Still image, refreshes every few seconds';
  mountMedia($('vMedia'), cam, { onFrame: () => { $('vAge').textContent = 'Updated ' + new Date().toLocaleTimeString(); } });
  $('vFav').textContent = isFav(cam) ? '★ Favorited' : '☆ Favorite';
  $('vFav').classList.toggle('on', isFav(cam));
  const near = nearest(cam.lat, cam.lon, 9).filter((c) => c !== cam).slice(0, 8);
  $('vNear').innerHTML = '';
  for (const c of near) {
    const el = document.createElement('div');
    el.className = 'nc';
    el.innerHTML = '<img loading="lazy" alt=""><span></span>';
    el.querySelector('img').src = thumb(c);
    el.querySelector('span').textContent = c.name;
    el.onclick = () => openViewer(c, true);
    $('vNear').append(el);
  }
  if (fly) map.flyTo([cam.lat, cam.lon], Math.max(map.getZoom(), 15), { duration: .8 });
}
$('vFav').onclick = () => { toggleFav(current); openViewer(current); };
$('vZoom').onclick = () => map.flyTo([current.lat, current.lon], 17);
$('vWall').onclick = () => {
  if (S.wall) { addToWall(current); return; }
  openWall([current, ...nearest(current.lat, current.lon, 9).filter((c) => c !== current)].slice(0, 9), 'Near ' + current.name);
};

function nearest(lat, lon, n, pool = S.cams.filter(visible)) {
  const k = Math.cos(lat * Math.PI / 180);
  return pool.map((c) => [((c.lat - lat) ** 2) + ((c.lon - lon) * k) ** 2, c]).sort((a, b) => a[0] - b[0]).slice(0, n).map((x) => x[1]);
}

// ------------------------------------------------------------------ search
let geoTimer, results = [], sel = 0;
$('q').addEventListener('input', () => {
  const q = $('q').value.trim();
  clearTimeout(geoTimer);
  if (!q) { $('results').hidden = true; return; }
  renderResults(q, null);
  if (q.length >= 3) geoTimer = setTimeout(async () => {
    const places = await api('/api/geocode?q=' + encodeURIComponent(q)).catch(() => []);
    if ($('q').value.trim() === q) renderResults(q, Array.isArray(places) ? places : []);
  }, 350);
});
$('q').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % Math.max(1, results.length);
    paintSel();
  } else if (e.key === 'Enter' && results[sel]) { results[sel].go(); }
  else if (e.key === 'Escape') { $('q').blur(); $('results').hidden = true; }
});
$('q').addEventListener('focus', () => { if ($('q').value.trim()) $('results').hidden = false; });
document.addEventListener('mousedown', (e) => { if (!e.target.closest('.search')) $('results').hidden = true; });

function renderResults(q, places) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const camHits = [];
  for (const c of S.cams) {
    if (!visible(c)) continue;
    if (words.every((w) => c.lname.includes(w))) { camHits.push(c); if (camHits.length >= 8) break; }
  }
  results = [];
  const box = $('results');
  box.innerHTML = '';
  const group = (title) => { const g = document.createElement('div'); g.className = 'grp'; g.textContent = title; box.append(g); };
  const add = (label, sub, go) => {
    const el = document.createElement('div');
    el.className = 'item';
    el.innerHTML = '<div><div class="lbl"></div><small></small></div>';
    el.querySelector('.lbl').textContent = label;
    el.querySelector('small').textContent = sub;
    const i = results.length;
    const run = () => { $('results').hidden = true; $('q').blur(); go(); };
    el.onmousedown = (e) => { e.preventDefault(); run(); };
    el.onmouseenter = () => { sel = i; paintSel(); };
    results.push({ el, go: run });
    box.append(el);
  };
  if (places && places.length) {
    group('Places');
    for (const p of places) add(p.name.split(',')[0], p.name.split(',').slice(1, 4).join(',').trim(), () => {
      const b = p.bbox;
      if (b.length === 4) map.flyToBounds([[b[0], b[2]], [b[1], b[3]]], { maxZoom: 14, duration: 1 });
      else map.flyTo([p.lat, p.lon], 12);
    });
  }
  if (camHits.length) {
    group('Cameras');
    for (const c of camHits) add(c.name, c.region, () => openViewer(c, true));
  }
  if (!results.length) {
    const el = document.createElement('div');
    el.className = 'grp';
    el.textContent = places ? 'No matches' : 'Searching…';
    box.append(el);
  }
  sel = places && places.length ? 0 : 0;
  paintSel();
  box.hidden = false;
}
function paintSel() { results.forEach((r, i) => r.el.classList.toggle('sel', i === sel)); }

// ------------------------------------------------------------------ monitor: drag a box
let boxStart = null, boxRect = null;
$('monitorBtn').onclick = () => (S.drawing ? stopDrawing() : startDrawing());
function startDrawing() {
  S.drawing = true;
  map.dragging.disable();
  map.boxZoom.disable();
  $('map').classList.add('drawing');
  $('monitorBtn').classList.add('on');
  $('drawHint').hidden = false;
}
function stopDrawing() {
  S.drawing = false;
  boxStart = null;
  if (boxRect) { boxRect.remove(); boxRect = null; }
  map.dragging.enable();
  map.boxZoom.enable();
  $('map').classList.remove('drawing');
  $('monitorBtn').classList.remove('on');
  $('drawHint').hidden = true;
}
map.on('mousedown', (e) => {
  if (!S.drawing) return;
  boxStart = e.latlng;
  boxRect = L.rectangle([boxStart, boxStart], { color: '#e5484d', weight: 2, fillOpacity: .08, interactive: false }).addTo(map);
});
map.on('mousemove', (e) => { if (S.drawing && boxStart) boxRect.setBounds([boxStart, e.latlng]); });
map.on('mouseup', (e) => {
  if (!S.drawing || !boxStart) return;
  const bounds = L.latLngBounds(boxStart, e.latlng);
  stopDrawing();
  const inside = S.cams.filter((c) => visible(c) && bounds.contains([c.lat, c.lon]));
  if (!inside.length) return toast('No cameras in that box');
  const c = bounds.getCenter();
  const picked = inside.length > WALL_MAX ? nearest(c.lat, c.lng, WALL_MAX, inside) : inside;
  if (inside.length > WALL_MAX) toast(`Showing the ${WALL_MAX} cameras nearest the middle of ${inside.length.toLocaleString()}`);
  openWall(picked, 'Monitor');
});

// ------------------------------------------------------------------ monitor wall
$('wCols').value = store.get('wCols', '0');
$('wRate').value = store.get('wRate', '10');
$('wLive').checked = store.get('wLive', true);
$('wCols').onchange = () => { store.set('wCols', $('wCols').value); layoutWall(); };
$('wRate').onchange = () => { store.set('wRate', $('wRate').value); remountWall(); };
$('wLive').onchange = () => { store.set('wLive', $('wLive').checked); remountWall(); };
$('wClose').onclick = closeWall;
$('wSave').onclick = () => {
  const name = prompt('Name this wall', S.wall.title === 'Monitor' ? '' : S.wall.title);
  if (!name) return;
  S.walls.unshift({ name, keys: S.wall.cams.map((c) => c.key) });
  S.wall.title = name;
  $('wTitle').textContent = name;
  saveState();
  renderFavs();
  toast('Wall saved');
};

function openWall(cams, title) {
  hide('viewer');
  S.wall = { cams: [...cams], title };
  $('wTitle').textContent = title;
  $('wall').hidden = false;
  $('wGrid').classList.remove('focus');
  remountWall();
}
function addToWall(cam) {
  if (S.wall.cams.includes(cam)) return toast('Already on the wall');
  S.wall.cams.push(cam);
  remountWall();
}
function closeWall() {
  $('wGrid').querySelectorAll('.tile').forEach(stopMedia);
  $('wGrid').innerHTML = '';
  $('wall').hidden = true;
  S.wall = null;
}
function remountWall() {
  const grid = $('wGrid');
  grid.querySelectorAll('.tile').forEach(stopMedia);
  grid.innerHTML = '';
  let players = 0;
  for (const cam of S.wall.cams) {
    const t = document.createElement('div');
    t.className = 'tile';
    t.innerHTML = '<div class="cap"><span></span><small></small></div><button class="rm" title="Remove from wall">&times;</button>';
    t.querySelector('.cap span').textContent = cam.name;
    t.querySelector('.cap small').textContent = cam.region;
    const useVideo = $('wLive').checked && cam.video && players < WALL_VIDEO_MAX;
    if (useVideo) players++;
    mountMedia(t, cam, { video: useVideo, refreshMs: +$('wRate').value * 1000 });
    t.onclick = () => {
      const focused = grid.classList.toggle('focus');
      t.classList.toggle('big', focused);
      if (!focused) grid.querySelectorAll('.big').forEach((b) => b.classList.remove('big'));
    };
    t.querySelector('.rm').onclick = (e) => {
      e.stopPropagation();
      stopMedia(t);
      t.remove();
      S.wall.cams = S.wall.cams.filter((c) => c !== cam);
      layoutWall();
    };
    grid.append(t);
  }
  layoutWall();
}
function layoutWall() {
  const n = S.wall ? S.wall.cams.length : 0;
  const cols = +$('wCols').value || Math.max(1, Math.min(8, Math.ceil(Math.sqrt(n * 1.4))));
  $('wGrid').style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  const live = S.wall ? S.wall.cams.filter((c) => c.video).length : 0;
  $('wCount').textContent = `${n} camera${n === 1 ? '' : 's'}` + (live ? ` · ${live} with video` : '');
}

// ------------------------------------------------------------------ keys
document.addEventListener('keydown', (e) => {
  const typing = /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
  if (e.key === 'Escape') {
    if (!$('svBig').hidden) return closeStreetBig();
    if (S.drawing) return stopDrawing();
    if (S.wall) {
      if ($('wGrid').classList.contains('focus')) { $('wGrid').classList.remove('focus'); $('wGrid').querySelectorAll('.big').forEach((b) => b.classList.remove('big')); return; }
      return closeWall();
    }
    for (const id of ['viewer', 'favs', 'layers']) if (!$(id).hidden) return hide(id);
  }
  if (typing || e.ctrlKey || e.altKey || e.metaKey || S.wall || !$('svBig').hidden) return;
  if (e.key === '/') { e.preventDefault(); $('q').focus(); $('q').select(); }
  else if (e.key === 'm' || e.key === 'M') S.drawing ? stopDrawing() : startDrawing();
  else if (e.key === 'l' || e.key === 'L') setLiveOnly(!S.liveOnly);
  else if (e.key === 'f' || e.key === 'F') $('favBtn').click();
  else if (e.key === 'c' || e.key === 'C') setFlockOn(!flockShown());
});

// ------------------------------------------------------------------ boot
Promise.all([loadState(), loadCameras()]).then(renderFavs).catch((e) => {
  $('stats').textContent = 'Could not reach the CamWorld server: ' + e.message;
});
// Restored only here, once the whole script has run: setGas shows a toast, and calling it up where the
// gas layer is defined (before toast's own variables exist) threw and stopped the page from loading.
if (gasOn) setGas(true);
