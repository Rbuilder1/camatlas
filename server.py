"""CamAtlas server: gathers open public camera catalogs into one list and serves the map UI.

Every source here is a public agency feed published for re-use (traffic/road-weather
cameras). Catalogs are cached on disk and refreshed in the background, so the map comes
up instantly after the first run. Stdlib only.

    python server.py                     serve on http://127.0.0.1:47835
"""
import base64
import collections
import concurrent.futures
import gzip
import hashlib
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
# Deliberately NOT %LOCALAPPDATA%: when anything is installed from inside a packaged
# (MSIX) app such as Claude desktop, writes under AppData are silently redirected into
# that app's private sandbox, so Explorer and the shortcuts can't see them at all.
DATA = os.environ.get("CAMATLAS_DATA") or os.path.join(os.path.expanduser("~"), "CamAtlas")
CACHE = os.path.join(DATA, "cache")
PORT = 47835
UA = "CamAtlas/1.0 (personal desktop camera map)"
CATALOG_TTL = 6 * 3600

os.makedirs(CACHE, exist_ok=True)


def fetch(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def fetch_json(url, **kw):
    return json.loads(fetch(url, **kw))


def fetch_post(url, form, timeout=50):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept-Encoding": "gzip", "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)


def cam(id, name, lat, lon, img, video=None, dir="", region=""):
    """One normalized camera: [id, name, lat, lon, image url, video url, direction, region]."""
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return [str(id), " ".join(str(name).split()), round(lat, 6), round(lon, 6), clean_url(img), clean_url(video),
            dir or "", region or ""]


def clean_url(u):
    """Agency feeds sometimes carry " https://..." (ND), "host/path.jpg" with no scheme (MI), or a bare
    "?123" cache-buster with no image at all (KS). Repair what can be repaired, drop the rest."""
    u = (u or "").strip()
    if not u or u.startswith(("http://", "https://", "/")):
        return u
    host = u.split("/", 1)[0]
    return "https://" + u if "." in host and " " not in host else ""


# ---------------------------------------------------------------- sources

def src_tfl(_):
    out = []
    for p in fetch_json("https://api.tfl.gov.uk/Place/Type/JamCam"):
        props = {a["key"]: a["value"] for a in p.get("additionalProperties", [])}
        if props.get("available") != "true":
            continue
        out.append(cam(p["id"], p["commonName"], p["lat"], p["lon"], props.get("imageUrl"),
                       props.get("videoUrl"), props.get("view"), "London"))
    return out


def src_caltrans(_):
    def district(d):
        url = f"https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json"
        rows = []
        for row in fetch_json(url).get("data", []):
            c = row.get("cctv") or {}
            if str(c.get("inService")).lower() != "true":
                continue
            loc, media = c.get("location") or {}, c.get("imageData") or {}
            img = (media.get("static") or {}).get("currentImageURL") or ""
            video = media.get("streamingVideoURL") or ""
            if not img.startswith("http"):
                continue
            rows.append(cam(f"D{d}-{c.get('index')}", loc.get("locationName", ""), loc.get("latitude"),
                            loc.get("longitude"), img, video if video.startswith("http") and ".m3u8" in video else "",
                            loc.get("direction"), f"{loc.get('nearbyPlace') or loc.get('county') or ''}, CA"))
        return rows
    out = []
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        for rows in pool.map(lambda d: _safe(district, d), range(1, 13)):
            out += rows
    return out


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return []


def src_austin(_):
    out = []
    for r in fetch_json("https://data.austintexas.gov/resource/b4k4-adkb.json?$limit=5000"):
        if r.get("camera_status") != "TURNED_ON" or not r.get("location"):
            continue
        lon, lat = r["location"]["coordinates"][:2]
        out.append(cam(r["camera_id"], r.get("location_name", ""), lat, lon,
                       f"https://cctv.austinmobility.io/image/{r['camera_id']}.jpg", region="Austin, TX"))
    return out


_sg = {"at": 0, "images": {}}
_sg_lock = threading.Lock()


def sg_images():
    """Singapore image URLs rotate every minute, so the map links to /snap/sg/<id> instead."""
    with _sg_lock:
        if time.time() - _sg["at"] > 50:
            data = fetch_json("https://api.data.gov.sg/v1/transport/traffic-images")
            cams = data["items"][0]["cameras"]
            _sg["images"] = {c["camera_id"]: c["image"] for c in cams}
            _sg["cams"] = cams
            _sg["at"] = time.time()
        return _sg


def src_singapore(_):
    return [cam(c["camera_id"], f"Singapore traffic camera {c['camera_id']}", c["location"]["latitude"],
                c["location"]["longitude"], f"/snap/sg/{c['camera_id']}", region="Singapore")
            for c in sg_images()["cams"]]


def src_hongkong(_):
    root = ET.fromstring(fetch("https://static.data.gov.hk/td/traffic-snapshot-images/code/Traffic_Camera_Locations_En.xml"))
    out = []
    for im in root.iter("image"):
        g = lambda k: (im.findtext(k) or "").strip()
        if g("latitude") and g("url"):
            out.append(cam(g("key"), g("description"), g("latitude"), g("longitude"), g("url"),
                           region=f"{g('district')}, Hong Kong"))
    return out


def src_finland(_):
    out = []
    data = fetch_json("https://tie.digitraffic.fi/api/weathercam/v1/stations")
    for f in data.get("features", []):
        p = f.get("properties") or {}
        if p.get("collectionStatus") != "GATHERING":
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        name = p.get("name", "").replace("_", " ")
        presets = [x for x in p.get("presets", []) if x.get("inCollection")]
        for i, pr in enumerate(presets):
            label = name + (f" (view {i + 1})" if len(presets) > 1 else "")
            out.append(cam(pr["id"], label, lat, lon, f"https://weathercam.digitraffic.fi/{pr['id']}.jpg", region="Finland"))
    return out


def src_nyc(_):
    return [cam(c["id"], c["name"], c["latitude"], c["longitude"], c["imageUrl"], region=f"{c.get('area', '')}, NYC")
            for c in fetch_json("https://webcams.nyctmc.org/api/cameras") if str(c.get("isOnline")).lower() == "true"]


def src_drivebc(_):
    out = []
    for c in fetch_json("https://www.drivebc.ca/api/webcams/"):
        if not (c.get("is_on") and c.get("should_appear")) or not c.get("location"):
            continue
        lon, lat = c["location"]["coordinates"][:2]
        out.append(cam(c["id"], c.get("name", ""), lat, lon, f"https://www.drivebc.ca/images/{c['id']}.jpg",
                       dir=c.get("orientation"), region=f"{c.get('region_name', '')}, BC"))
    return out


def carsprogram(slug, state, abbr, skip_owner=None):
    """State 511 sites on the newer CARS/carsprogram.org platform (GeoJSON map-features).
    Each view carries an HLS stream plus a snapshot (videoPreviewUrl). `skip_owner` drops cameras
    that duplicate another source (e.g. NYC DOT, already covered by `nyc`)."""
    def fetch_cp(_):
        out = []
        data = fetch_json(f"https://api-511x-{slug}.carsprogram.org/cameras/map-features")
        for f in data.get("features", []):
            p = f.get("properties") or {}
            if not p.get("public") or (skip_owner and p.get("cameraOwner") == skip_owner):
                continue
            lon, lat = f["geometry"]["coordinates"][:2]
            img = video = ""
            for v in p.get("views") or []:
                if v.get("broken"):
                    continue
                img = img or v.get("videoPreviewUrl") or (v.get("url") if v.get("type") == "STILL_IMAGE" else "")
                if v.get("type") != "STILL_IMAGE" and str(v.get("url", "")).startswith("http"):
                    video = video or v["url"]
            if not img:
                continue
            loc = p.get("location") or {}
            city = loc.get("cityReference")
            where = (city if city and city != "Unknown" else None) or loc.get("routeId") or p.get("route") or ""
            out.append(cam(p.get("id"), p.get("name") or p.get("tooltip") or f"{abbr} camera", lat, lon, img, video,
                           region=f"{where}, {abbr}" if where else state))
        return out
    return fetch_cp


def iteris_nested(code, state):
    """Smaller Iteris 511 states (SD, MT) whose GeoJSON nests several still-image views per site."""
    def fetch_it(_):
        out = []
        data = fetch_json(f"https://{code}.cdn.iteris-atis.com/geojson/icons/metadata/icons.cameras.geojson")
        for f in data.get("features", []):
            p = f.get("properties") or {}
            lon, lat = f["geometry"]["coordinates"][:2]
            for v in p.get("cameras") or []:
                if not v.get("image"):
                    continue
                name = v.get("description") or p.get("name") or p.get("route") or f"{state} camera"
                out.append(cam(f"{p.get('name')}-{v.get('id')}", name, lat, lon, v["image"], region=state))
        return out
    return fetch_it


def src_southcarolina(_):
    """SCDOT (511sc.org, Iteris). Keyless GeoJSON; HLS video + snapshot for every camera."""
    out = []
    data = fetch_json("https://sc.cdn.iteris-atis.com/geojson/icons/metadata/icons.cameras.geojson")
    for f in data.get("features", []):
        p = f.get("properties") or {}
        if not p.get("active") or p.get("problem_stream") or not p.get("image_url"):
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        video = p.get("https_url") or ""
        out.append(cam(p.get("name") or p.get("guid"), p.get("description") or p.get("route") or "SC camera",
                       lat, lon, p["image_url"], video if video.startswith("http") else "",
                       dir=p.get("direction"), region=f"{p.get('jurisdiction', '')}, SC"))
    return out


def cars511(host, state=""):
    """US state 511 sites on the shared Castle Rock vendor (List/GetData/Cameras, DataTables).
    Newer installs (NV, NC) only answer the JSON `query=` form and older ones the form POST, so
    each page tries the JSON form first. Streams marked public are used as live video."""
    def page_of(start, size):
        q = json.dumps({"columns": [{"data": None, "name": ""}], "order": [{"column": 0, "dir": "asc"}],
                        "start": start, "length": size, "search": {"value": ""}})
        for attempt in range(2):
            try:
                d = fetch_json(f"https://{host}/List/GetData/Cameras?query={urllib.parse.quote(q)}&lang=en-US",
                               headers={"X-Requested-With": "XMLHttpRequest"}, timeout=50)
                if d.get("data") or not d.get("recordsTotal"):
                    return d
                break  # answered, but only the older form gives rows on this install
            except Exception:
                time.sleep(1)
        return fetch_post(f"https://{host}/List/GetData/Cameras", {
            "draw": 1, "start": start, "length": size, "order[0][column]": 0, "order[0][dir]": "asc", "columns[0][data]": 0})

    def fetch_cars(_):
        out, start, page = [], 0, 100  # most installs cap each response at 100; some (e.g. Alberta) lower
        while True:
            try:
                d = page_of(start, page)
            except Exception:
                time.sleep(1)
                try:
                    d = page_of(start, page)
                except Exception:
                    if page > 10:  # this install's cap is below our guess — halve it and keep going
                        page //= 2
                        continue
                    if out:  # keep the partial list rather than failing the whole source
                        return out
                    raise
            rows = d.get("data", [])
            for r in rows:
                imgs = r.get("images") or []
                if not imgs or imgs[0].get("disabled") or imgs[0].get("blocked"):
                    continue
                im = imgs[0]
                wkt = (((r.get("latLng") or {}).get("geography") or {}).get("wellKnownText") or "")
                m = wkt.replace("POINT (", "").replace(")", "").split()
                if len(m) != 2:
                    continue
                video = im.get("videoUrl") or ""
                if im.get("isVideoAuthRequired") or im.get("videoDisabled") or ".m3u8" not in video:
                    video = ""
                img = im.get("imageUrl") or ""
                region = ", ".join(x for x in [r.get("county"), r.get("state")] if x) or state
                out.append(cam(r.get("id"), r.get("location") if r.get("location") not in (None, "", "N/A") else
                               (im.get("description") or r.get("roadway") or f"Camera {r.get('id')}"),
                               m[1], m[0], img if img.startswith("http") else f"https://{host}{img}", video,
                               dir=r.get("direction") if r.get("direction") != "Unknown" else "", region=region))
            start += len(rows)
            if not rows or start >= d.get("recordsTotal", 0) or start > 20000:
                return out
            time.sleep(0.12)  # these vendor sites throw 500s under rapid sequential paging
    return fetch_cars


def src_newzealand(_):
    root = ET.fromstring(fetch("https://trafficnz.info/service/traffic/rest/4/cameras/all"))
    out = []
    for c in root.iter("camera"):
        g = lambda k: (c.findtext(k) or "").strip()
        if g("offline") == "true" or g("underMaintenance") == "true" or not g("latitude"):
            continue
        out.append(cam(g("id"), g("name") or g("description"), g("latitude"), g("longitude"),
                       "https://trafficnz.info" + g("imageUrl"), dir=g("direction"), region="New Zealand"))
    return out


def src_nsw(_):
    # Live Traffic NSW's one public feed mixes incidents and roadworks in with the cameras.
    out = []
    for f in fetch_json("https://www.livetraffic.com/datajson/all-feeds-web.json"):
        if f.get("eventType") != "liveCams":
            continue
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        out.append(cam(f["id"], p.get("title"), lat, lon, p.get("href"), dir=p.get("direction"),
                       region="New South Wales"))
    return out


def src_spain(_):
    # DGT's national access point publishes every camera as DATEX II; namespaces vary, so match on tag tails.
    root = ET.fromstring(fetch("https://nap.dgt.es/datex2/v3/dgt/DevicePublication/camaras_datex2_v36.xml", timeout=90))
    out = []
    for d in root.iter():
        if not d.tag.endswith("}device"):
            continue
        g = {e.tag.rsplit("}", 1)[-1]: (e.text or "").strip() for e in d.iter()}
        if g.get("typeOfDevice") != "camera" or not g.get("latitude"):
            continue
        name = f"{g.get('roadName', '')} km {g.get('kilometerPoint', '?')}"
        if g.get("roadDestination"):
            name += f" towards {g['roadDestination'].title()}"
        out.append(cam(d.get("id"), name, g["latitude"], g["longitude"], g.get("deviceUrl"),
                       region=(g.get("province") or "Spain").title()))
    return out


def src_iceland(_):
    out = []
    for c in fetch_json("https://gagnaveita.vegagerdin.is/api/vefmyndavelar2014_1"):
        out.append(cam(c["Slod"].rsplit("/", 1)[-1], f"{c['Myndavel']}: {c['Skyring']}", c["Breidd"], c["Lengd"],
                       c["Slod"], region="Iceland"))
    return out


def src_usgs(_):
    # USGS HIVIS river/stream-gauge cameras (NIMS). Cameras silent for a week are left out.
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 7 * 86400))
    out = []
    for c in fetch_json("https://api.waterdata.usgs.gov/nims/v0/cameras?enabled=true", timeout=60):
        if c.get("hideCam") or (c.get("newestImageDT") or "") < cutoff or not c.get("smallDir"):
            continue
        out.append(cam("usgs_" + c["camId"], c.get("camName") or c["camId"], c["lat"], c["lng"],
                       f"{c['smallDir']}{c['camId']}_newest.jpg", region=c.get("stateAbrv") or "USGS"))
    return out


def src_michigan(_):
    import re
    out = []
    for r in fetch_json("https://mdotjboss.state.mi.us/MiDrive/camera/list"):
        blob = r.get("county", "")
        lat, lon = re.search(r"lat=([-\d.]+)", blob), re.search(r"lon=([-\d.]+)", blob)
        img = re.search(r'src="([^"]+)"', r.get("image", ""))
        if not (lat and lon and img):
            continue
        county = re.sub(r"\s*<a.*", "", blob).strip()
        name = " ".join(x for x in [r.get("route", "").strip(), r.get("location", "").strip()] if x)
        out.append(cam(img.group(1), name or "Michigan camera", lat.group(1), lon.group(1),
                       img.group(1).split("?")[0], region=f"{county}, MI"))
    return out


def gql511(host, bbox, state, video=True):
    """Newer Castle Rock 511 sites (KS, IN, NE, MA) whose map runs on /api/graphql. One query over the
    whole state at a street-level zoom returns every camera unclustered. `bbox` = (north, south, east, west).
    `video=False` where the streams can't be played (KS needs a login, MA's links expire in minutes)."""
    q = """query ($input: MapFeaturesArgs!) { mapFeaturesQuery(input: $input) { mapFeatures { tooltip uri
      features { geometry } ... on Camera { views(limit: 5) { category ... on CameraView { url sources { type src } } } } } } }"""
    n, s, e, w = bbox

    def fetch_gql(_):
        body = json.dumps({"query": q, "variables": {"input": {"north": n, "south": s, "east": e, "west": w,
                                                                "zoom": 14, "layerSlugs": ["normalCameras"]}}}).encode()
        req = urllib.request.Request(f"https://{host}/api/graphql", data=body,
                                     headers={"User-Agent": UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            feats = json.loads(r.read())["data"]["mapFeaturesQuery"]["mapFeatures"]
        out = []
        for f in feats:
            if not str(f.get("uri", "")).startswith("camera/") or not f.get("features"):
                continue
            lon, lat = f["features"][0]["geometry"]["coordinates"][:2]
            views = f.get("views") or []
            img = next((v["url"] for v in views if v.get("url")), "")
            hls = next((src["src"] for v in views for src in (v.get("sources") or [])
                        if "mpegurl" in str(src.get("type", "")).lower() and src.get("src")), "")
            out.append(cam(f["uri"].split("/")[1], f.get("tooltip") or f"{state} camera", lat, lon, img,
                           hls if video else "", region=state))
        return out
    return fetch_gql


def arcgis_features(url, where="1=1"):
    """Every feature from an ArcGIS Feature/MapServer layer, paged, in lat/lon."""
    out, offset = [], 0
    while True:
        q = urllib.parse.urlencode({"where": where, "outFields": "*", "returnGeometry": "true", "outSR": 4326,
                                    "f": "json", "resultOffset": offset, "resultRecordCount": 1000})
        for attempt in range(3):  # ArcGIS Online drops the odd connection under load
            try:
                d = fetch_json(f"{url}/query?{q}", timeout=90)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 + attempt * 3)
        feats = d.get("features") or []
        out += feats
        offset += len(feats)
        if not feats or not d.get("exceededTransferLimit") or offset > 50000:
            return out


def src_iowa(_):
    """Iowa DOT open data: snapshots for every camera, HLS for most."""
    out = []
    for f in arcgis_features("https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/Traffic_Cameras_View/FeatureServer/0"):
        a = f["attributes"]
        if not a.get("ImageURL"):
            continue
        video = a.get("VideoURL") or ""
        out.append(cam(a.get("COMMON_ID") or a.get("FID"), a.get("Desc_") or a.get("ImageName"), a["latitude"], a["longitude"],
                       a["ImageURL"], video if ".m3u8" in video else "",
                       region=f"{a['Route']}, IA" if a.get("Route") else "Iowa"))
    return out


def src_illinois(_):
    """IDOT camera layer (snapshots served by Travel Midwest)."""
    out = []
    for f in arcgis_features("https://services2.arcgis.com/aIrBD8yn1TDTEXoz/ArcGIS/rest/services/TrafficCamerasTM_Public/FeatureServer/0"):
        a, g = f["attributes"], f.get("geometry") or {}
        if not a.get("SnapShot") or a.get("TooOld") == "true":
            continue
        out.append(cam(a.get("OBJECTID"), a.get("CameraLocation"), g.get("y", a.get("y")), g.get("x", a.get("x")),
                       a["SnapShot"], dir=a.get("CameraDirection") if a.get("CameraDirection") != "NONE" else "",
                       region="Illinois"))
    return out


def src_kentucky(_):
    out = []
    for f in arcgis_features("https://services2.arcgis.com/CcI36Pduqd0OR4W9/arcgis/rest/services/trafficCamerasCur_Prd/FeatureServer/0"):
        a = f["attributes"]
        if a.get("snapshot"):
            out.append(cam(a.get("OBJECTID"), a.get("description") or a.get("name"), a["latitude"], a["longitude"],
                           a["snapshot"], region=f"{a['county']} Co., KY" if a.get("county") else "Kentucky"))
    return out


def src_washington(_):
    """WSDOT highway cameras KML (images.wsdot.wa.gov)."""
    import re
    root = ET.fromstring(fetch("https://www.wsdot.wa.gov/traffic/api/highwaycameras/kml.aspx", timeout=90))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    out = []
    for p in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        m = re.search(r'src="([^"]+)"', p.findtext("k:description", "", ns))
        coords = (p.findtext("k:Point/k:coordinates", "", ns) or "").split(",")
        if m and len(coords) >= 2:
            out.append(cam(p.get("id", "").replace("ID ", ""), p.findtext("k:name", "", ns), coords[1], coords[0],
                           m.group(1), region="Washington"))
    return out


def src_oregon(_):
    out = []
    for f in fetch_json("https://tripcheck.com/Scripts/map/data/cctvinventory.js").get("features", []):
        a = f["attributes"]
        if a.get("filename"):
            route = (a.get("route") or "").strip()
            out.append(cam(a["cameraId"], a.get("title"), a["latitude"], a["longitude"],
                           "https://tripcheck.com/RoadCams/cams/" + urllib.parse.quote(a["filename"]),
                           region=f"{route}, OR" if route else "Oregon"))
    return out


def src_maryland(_):
    """MDOT SHA CHART: live HLS only (no stills), so these show a video placeholder in lists."""
    out = []
    for c in fetch_json("https://chart.maryland.gov/DataFeeds/GetCamerasJson"):
        if c.get("commMode") != "ONLINE" or not c.get("cctvIp"):
            continue
        area = ", ".join(c.get("cameraCategories") or [])
        out.append(cam(c["id"], c.get("description") or c.get("name"), c["lat"], c["lon"], "",
                       f"https://{c['cctvIp']}/rtplive/{c['id']}/playlist.m3u8",
                       region=f"{area}, MD" if area else "Maryland"))
    return out


def src_delaware(_):
    out = []
    for c in fetch_json("https://tmc.deldot.gov/json/videocamera.json").get("videoCameras", []):
        hls = (c.get("urls") or {}).get("m3u8s")
        if c.get("enabled") and c.get("status") == "Active" and hls:
            out.append(cam(c["id"], c.get("title"), c["lat"], c["lon"], "", hls,
                           region=f"{c['county']} Co., DE" if c.get("county") else "Delaware"))
    return out


def src_missouri(_):
    out = []
    for c in fetch_json("https://traveler.modot.org/timconfig/feed/desktop/StreamingCams2.json"):
        hls = c.get("html") or ""
        if ".m3u8" in hls:
            out.append(cam(hls.rsplit("/", 2)[-2], c.get("location"), c["y"], c["x"], "", hls, region="Missouri"))
    return out


def src_alabama(_):
    out = []
    for c in fetch_json("https://api.algotraffic.com/v4.0/Cameras", timeout=60):
        loc = c.get("location") or {}
        if c.get("accessLevel") != "Public" or not c.get("snapshotImageUrl"):
            continue
        name = " @ ".join(x for x in [loc.get("displayRouteDesignator"), loc.get("displayCrossStreet")] if x and x != "Unknown")
        out.append(cam(c["id"], name or f"Alabama camera {c['id']}", loc.get("latitude"), loc.get("longitude"),
                       c["snapshotImageUrl"], (c.get("playbackUrls") or {}).get("hls"), dir=loc.get("direction"),
                       region=f"{loc['city']}, AL" if loc.get("city") else "Alabama"))
    return out


def src_northdakota(_):
    out = []
    for f in fetch_json("https://travelfiles.dot.nd.gov/geojson_nc/cameras.json").get("features", []):
        lon, lat = f["geometry"]["coordinates"][:2]
        p = f.get("properties") or {}
        for i, v in enumerate(p.get("Cameras") or []):
            if v.get("FullPath"):
                out.append(cam(f"{f.get('id')}-{i}", v.get("Description"), lat, lon, v["FullPath"], dir=v.get("Direction"),
                               region=f"{p['Region']}, ND" if p.get("Region") else "North Dakota"))
    return out


def src_newmexico(_):
    out = []
    for c in fetch_json("https://servicev4.nmroads.com/RealMapWAR/GetCameraInfo").get("cameraInfo", []):
        if c.get("enabled") and c.get("snapshotFile"):
            out.append(cam(c.get("name"), c.get("title"), c["lat"], c["lon"], c["snapshotFile"],
                           region=f"{c['grouping']}, NM" if c.get("grouping") else "New Mexico"))
    return out


def src_ohio(_):
    """OHGO (ODOT): one site can carry several directional stills."""
    out = []
    for s in fetch_json("https://api.ohgo.com/roadmarkers/cameras", timeout=60):
        for i, v in enumerate(s.get("Cameras") or []):
            img = (v.get("LargeURL") or v.get("SmallURL") or "").replace(" ", "%20")
            if img:
                out.append(cam(f"{s['Id']}-{i}", s.get("Description") or s.get("Location"), s["Latitude"], s["Longitude"],
                               img, dir=v.get("Direction") if v.get("Direction") != "View" else "", region="Ohio"))
    return out


TX_DISTRICTS = "ABL AMA ATL AUS BMT BRY BWD CHS CRP DAL ELP FTW HOU LBB LFK LRD ODA PAR PHR SAT SJT TYL WAC WFS YKM".split()


def src_texas(_):
    """TxDOT ITS, all 25 districts. Snapshots come back as base64 inside JSON, so the map points at
    /snap/tx (see Handler), which decodes them into a normal image."""
    def district(d):
        data = fetch_json(f"https://its.txdot.gov/its/DistrictIts/GetCctvStatusListByDistrict?districtCode={d}", timeout=90)
        return d, [c for v in (data.get("roadwayCctvStatuses") or {}).values() for c in v]
    out = []
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        for d, cams in ex.map(district, TX_DISTRICTS):
            for c in cams:
                if not c.get("hasSnapshot") or "Online" not in str(c.get("statusDescription") or "Online"):
                    continue
                q = urllib.parse.urlencode({"d": d, "id": c["icd_Id"]})
                out.append(cam(f"{d}-{c['icd_Id']}", c.get("name") or c["icd_Id"], c["latitude"], c["longitude"],
                               f"/snap/tx?{q}", dir=c.get("dirDescription"), region=f"{d} district, TX"))
    return out


def tx_snapshot(district, icd_id):
    q = urllib.parse.urlencode({"icdId": icd_id, "districtCode": district})
    data = fetch_json(f"https://its.txdot.gov/its/DistrictIts/GetCctvSnapshotByIcdId?{q}", timeout=30)
    return base64.b64decode(data["snippet"])


SOURCES = [
    # id, label, fetcher, attribution
    ("caltrans", "Caltrans (California)", src_caltrans, "Caltrans cwwp2.dot.ca.gov"),
    ("nyc", "NYC DOT (New York City)", src_nyc, "NYC DOT webcams.nyctmc.org"),
    ("ny511", "511NY (NY State & Hudson Valley)", carsprogram("nysdot", "New York", "NY", skip_owner="NYC DOT"), "511NY / NYSDOT, 511ny.org"),
    ("minnesota", "MnDOT (Minnesota)", carsprogram("mn", "Minnesota", "MN"), "Minnesota DOT, 511mn.org"),
    ("colorado", "CDOT (Colorado)", carsprogram("co", "Colorado", "CO"), "Colorado DOT, COtrip.org"),
    ("austin", "City of Austin, TX", src_austin, "City of Austin, TX, data.austintexas.gov"),
    ("florida", "FL511 (Florida)", cars511("fl511.com"), "Florida DOT, fl511.com"),
    # 511 Georgia (cars511("511ga.org")) is off: since 2026-10 every /map/Cctv/<id> still is the same
    # "No live camera feed at this time" picture, even in a browser, and the streams answer 401 without a
    # GDOT login. All 4,331 cameras only ever showed as offline. To check if it's back: fetch two
    # /map/Cctv/<id> images and see whether they still match byte for byte.
    ("southcarolina", "SCDOT (South Carolina)", src_southcarolina, "South Carolina DOT, 511sc.org"),
    ("newengland", "511 New England (ME/NH/VT)", cars511("www.newengland511.org"),
     "New England 511 — Maine DOT, NHDOT, VTrans, newengland511.org"),
    ("pennsylvania", "511PA (Pennsylvania)", cars511("511pa.com"), "PennDOT, 511pa.com"),
    ("alaska", "511 Alaska", cars511("511.alaska.gov"), "Alaska DOT&PF, 511.alaska.gov"),
    ("montana", "MDT (Montana)", iteris_nested("mt", "Montana"), "Montana DOT, roadreport.mdt.mt.gov"),
    ("southdakota", "SDDOT (South Dakota)", iteris_nested("sd", "South Dakota"), "South Dakota DOT, sd511.org"),
    ("ontario", "511 Ontario (Canada)", cars511("511on.ca"), "Ontario MTO, 511on.ca"),
    ("newbrunswick", "511 New Brunswick (Canada)", cars511("511.gnb.ca"), "New Brunswick DTI, 511.gnb.ca"),
    ("newfoundland", "511 Newfoundland (Canada)", cars511("511nl.ca"), "Newfoundland & Labrador TI, 511nl.ca"),
    ("yukon", "511 Yukon (Canada)", cars511("511yukon.ca"), "Government of Yukon, 511yukon.ca"),
    ("alberta", "511 Alberta (Canada)", cars511("511.alberta.ca", "Alberta"), "Alberta Transportation, 511.alberta.ca"),
    ("novascotia", "511 Nova Scotia (Canada)", cars511("511.novascotia.ca", "Nova Scotia"), "Nova Scotia Public Works, 511.novascotia.ca"),
    ("manitoba", "511 Manitoba (Canada)", cars511("www.manitoba511.ca", "Manitoba"), "Manitoba Transportation, manitoba511.ca"),
    ("saskatchewan", "Highway Hotline (Saskatchewan)", cars511("hotline.gov.sk.ca", "Saskatchewan"),
     "Saskatchewan Highways, hotline.gov.sk.ca"),
    ("wisconsin", "511 Wisconsin", cars511("511wi.gov"), "Wisconsin DOT, 511wi.gov"),
    ("texas", "TxDOT (Texas)", src_texas, "Texas DOT, its.txdot.gov"),
    ("washington", "WSDOT (Washington)", src_washington, "Washington State DOT, wsdot.wa.gov"),
    ("oregon", "ODOT TripCheck (Oregon)", src_oregon, "Oregon DOT, TripCheck.com"),
    ("arizona", "AZ511 (Arizona)", cars511("az511.gov", "Arizona"), "Arizona DOT, az511.gov"),
    ("nevada", "NVRoads (Nevada)", cars511("nvroads.com", "Nevada"), "Nevada DOT, nvroads.com"),
    ("utah", "UDOT (Utah)", cars511("udottraffic.utah.gov", "Utah"), "Utah DOT, udottraffic.utah.gov"),
    ("idaho", "511 Idaho", cars511("511.idaho.gov", "Idaho"), "Idaho Transportation Dept, 511.idaho.gov"),
    ("newmexico", "NMRoads (New Mexico)", src_newmexico, "New Mexico DOT, nmroads.com"),
    ("northdakota", "NDDOT (North Dakota)", src_northdakota, "North Dakota DOT, dot.nd.gov"),
    ("nebraska", "511 Nebraska", gql511("new.511.nebraska.gov", (43.1, 39.9, -95.2, -104.1), "Nebraska"),
     "Nebraska DOT, 511.nebraska.gov"),
    ("kansas", "KanDrive (Kansas)", gql511("www.kandrive.gov", (40.1, 36.9, -94.5, -102.1), "Kansas", video=False),
     "Kansas DOT / KC Scout, kandrive.gov"),
    ("iowa", "Iowa DOT", src_iowa, "Iowa DOT open data, 511ia.org"),
    ("missouri", "MoDOT (Missouri)", src_missouri, "Missouri DOT, traveler.modot.org"),
    ("illinois", "IDOT (Illinois)", src_illinois, "Illinois DOT / Travel Midwest"),
    ("indiana", "511 Indiana", gql511("511in.org", (41.8, 37.7, -84.7, -88.2), "Indiana"), "Indiana DOT, 511in.org"),
    ("ohio", "OHGO (Ohio)", src_ohio, "Ohio DOT, ohgo.com"),
    ("kentucky", "KYTC (Kentucky)", src_kentucky, "Kentucky Transportation Cabinet"),
    ("louisiana", "511 Louisiana", cars511("511la.org", "Louisiana"), "Louisiana DOTD, 511la.org"),
    ("alabama", "ALGO Traffic (Alabama)", src_alabama, "Alabama DOT, algotraffic.com"),
    ("northcarolina", "DriveNC (North Carolina)", cars511("drivenc.gov", "North Carolina"), "NCDOT, drivenc.gov"),
    ("maryland", "CHART (Maryland)", src_maryland, "Maryland SHA CHART, chart.maryland.gov"),
    ("delaware", "DelDOT (Delaware)", src_delaware, "Delaware DOT, deldot.gov"),
    ("connecticut", "CTroads (Connecticut)", cars511("ctroads.org", "Connecticut"), "Connecticut DOT, ctroads.org"),
    ("massachusetts", "Mass511 (Massachusetts)", gql511("mass511.com", (42.9, 41.2, -69.9, -73.6), "Massachusetts", video=False),
     "MassDOT, mass511.com"),
    ("michigan", "MDOT (Michigan)", src_michigan, "Michigan DOT, MiDrive"),
    ("usgs", "USGS river cameras (HIVIS)", src_usgs, "U.S. Geological Survey, apps.usgs.gov/hivis"),
    ("newzealand", "NZTA (New Zealand)", src_newzealand, "Waka Kotahi NZ Transport Agency, trafficnz.info"),
    ("nsw", "Live Traffic NSW (Australia)", src_nsw, "Transport for NSW, livetraffic.com"),
    ("drivebc", "DriveBC (British Columbia)", src_drivebc, "DriveBC.ca, Province of British Columbia"),
    ("tfl", "TfL JamCams (London)", src_tfl, "Powered by TfL Open Data. Contains OS data © Crown copyright and database rights"),
    ("finland", "Digitraffic (Finland)", src_finland, "Fintraffic / digitraffic.fi, CC BY 4.0"),
    ("spain", "DGT (Spain)", src_spain, "Dirección General de Tráfico, nap.dgt.es"),
    ("iceland", "Vegagerðin (Iceland)", src_iceland, "Icelandic Road and Coastal Administration, vegagerdin.is"),
    ("hongkong", "Transport Dept (Hong Kong)", src_hongkong, "Transport Department, HKSAR, data.gov.hk"),
    ("singapore", "LTA (Singapore)", src_singapore, "Land Transport Authority, data.gov.sg"),
]


# ---------------------------------------------------------------- state

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f)
    os.replace(tmp, path)


SETTINGS_FILE = os.path.join(DATA, "settings.json")
STATE_FILE = os.path.join(DATA, "state.json")  # favorites + saved monitor walls
settings = load_json(SETTINGS_FILE, {})
catalog = {}  # source id -> {"cameras": [...], "updated": t, "error": str}
refreshing = set()
lock = threading.Lock()
# 50 sources refreshing at once swamp a home connection (timeouts, dropped TLS handshakes), so
# only a few download at a time; the rest wait their turn.
fetch_slots = threading.BoundedSemaphore(6)

for sid, *_ in SOURCES:
    catalog[sid] = load_json(os.path.join(CACHE, sid + ".json"), {"cameras": [], "updated": 0, "error": ""})


def refresh(sid):
    with lock:
        if sid in refreshing:
            return
        refreshing.add(sid)
    fn = next(s[2] for s in SOURCES if s[0] == sid)
    try:
        with fetch_slots:
            cams = [c for c in fn(settings) if c and (c[4] or c[5])]  # a still, or live video
        if not cams:  # a hiccup that returns nothing must not wipe out the last good list
            raise RuntimeError("returned no cameras")
        entry = {"cameras": cams, "updated": time.time(), "tried": time.time(), "error": ""}
        save_json(os.path.join(CACHE, sid + ".json"), entry)
    except Exception as e:  # keep the last good list on failure
        entry = {**catalog[sid], "error": str(e) or type(e).__name__, "tried": time.time()}
    with lock:
        catalog[sid] = entry
        refreshing.discard(sid)


def refresh_stale(force=False):
    for sid, *_ in SOURCES:
        c, now = catalog[sid], time.time()
        if force or (now - c.get("updated", 0) > CATALOG_TTL and now - c.get("tried", 0) > 300):
            threading.Thread(target=refresh, args=(sid,), daemon=True).start()


def scheduler():
    while True:
        refresh_stale()
        if time.time() - flock.get("updated", 0) > FLOCK_TTL and time.time() - flock.get("tried", 0) > 3600:
            threading.Thread(target=refresh_flock, daemon=True).start()
        time.sleep(300)


# ---- Flock / ALPR surveillance-camera LOCATIONS (not watchable feeds) --------
# These are automated license-plate readers run by police and HOAs; there is no public
# video. Their reported locations are crowd-documented in OpenStreetMap by the DeFlock
# project (deflock.org). We plot the points as a transparency layer, loaded on demand.
FLOCK_URL = "https://data.dontgetflocked.com/cameras.geojson.gz"
FLOCK_TTL = 7 * 24 * 3600
flock = load_json(os.path.join(CACHE, "flock.json"), {"pts": [], "updated": 0, "error": ""})


def refresh_flock():
    global flock
    with lock:
        if "flock" in refreshing:
            return
        refreshing.add("flock")
    try:
        data = fetch_json(FLOCK_URL, timeout=120)
        pts = []
        for f in data.get("features", []):
            p = f.get("properties") or {}
            if "flock" not in (p.get("brand") or p.get("manufacturer") or "").lower():
                continue
            c = (f.get("geometry") or {}).get("coordinates") or []
            if len(c) < 2:
                continue
            lon, lat = c[0], c[1]
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            pts.append([round(lat, 5), round(lon, 5), (p.get("ref") or "")[:40]])
        entry = {"pts": pts, "updated": time.time(), "tried": time.time(), "error": ""}
        save_json(os.path.join(CACHE, "flock.json"), entry)
    except Exception as e:
        entry = {**flock, "error": str(e) or type(e).__name__, "tried": time.time()}
    with lock:
        flock = entry
        refreshing.discard("flock")


_geo_cache = {}


def geocode(q):
    q = q.strip()[:120]
    if q not in _geo_cache:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 6})
        rows = fetch_json(url, headers={"Accept-Language": "en"}, timeout=15)
        _geo_cache[q] = [{"name": r["display_name"], "lat": float(r["lat"]), "lon": float(r["lon"]),
                          "bbox": [float(x) for x in r.get("boundingbox", [])]} for r in rows]
    return _geo_cache[q]


# ---------------------------------------------------------------- http

# ---- Discovery: search open-data catalogs for camera feeds that aren't on the map yet ----
# ArcGIS Online and Socrata index thousands of public agency datasets. A search turns up
# far more noise than cameras, so each candidate has to prove itself: point locations, plus
# a field whose links really answer with images (or HLS streams). The user adds the ones
# they want; added ones become ordinary sources, saved in discovered.json.
DISCOVERED_FILE = os.path.join(DATA, "discovered.json")
DISCOVER_TERMS = ["traffic cameras", "traffic camera images", "webcams", "cctv cameras", "road cameras",
                  "camera snapshots", "weather cameras", "live cameras"]
_disc_saved = load_json(DISCOVERED_FILE, {})
discovery = {"running": False, "checked": 0, "total": 0, "status": "", "found": _disc_saved.get("found", []),
             "seen": set(_disc_saved.get("seen", []))}
IMG_EXT = re.compile(r"\.(jpe?g|png|gif|webp)(\?|$)", re.I)
# A camera search also returns flood zones, sewer lines and speed-camera tickets; only datasets whose
# title sounds like pictures are worth the requests it takes to test them.
CAM_TITLE = re.compile(r"cam|cctv|webcam|snapshot|image|video", re.I)


def save_discovery():
    with lock:
        value = {"found": discovery["found"], "seen": sorted(discovery["seen"])}
    save_json(DISCOVERED_FILE, value)


def known_datasets():
    """ArcGIS services and Socrata datasets the built-in sources already read, taken from the string
    constants of this module's own functions (Android ships no server.py to read, only bytecode)."""
    def strings(code):
        for c in code.co_consts:
            if isinstance(c, str):
                yield c
            elif hasattr(c, "co_consts"):
                yield from strings(c)
    text = " ".join(s for f in list(globals().values()) if hasattr(f, "__code__") for s in strings(f.__code__))
    known = {s.lower() for s in re.findall(r"arcgis\.com/\w+/arcgis/rest/services/(\w+)", text, re.I)}
    return known | set(re.findall(r"/resource/([a-z0-9]{4}-[a-z0-9]{4})", text))


def _flat(v):
    """Socrata wraps URL columns as {"url": ...}; everything else compares as a plain string."""
    if isinstance(v, dict):
        v = v.get("url", "")
    return v.strip() if isinstance(v, str) else ""


def pick_fields(rows):
    """Which columns hold the image link, the stream link, the name and the id. None if no column
    looks like it links to pictures or streams at all."""
    if not rows:
        return None
    cols = {k for r in rows for k in r}
    n, imgs, video = len(rows), [], None
    for k in cols:
        vals = [_flat(r.get(k)) for r in rows]
        urls = [v for v in vals if v.startswith(("http://", "https://"))]
        if len(urls) < n * 0.6:
            continue
        if sum(".m3u8" in u for u in urls) >= len(urls) * 0.6:
            video = video or k
            continue
        score = 3 * (sum(bool(IMG_EXT.search(u)) for u in urls) >= len(urls) * 0.6)
        score += 2 * bool(re.search(r"image|img|snap|photo|pic|still|jpg|cam", k, re.I))
        score -= 5 * (sum(bool(re.search(r"\.(html?|aspx?)(\?|$)", u, re.I)) for u in urls) >= len(urls) * 0.5)
        if score > 0:
            imgs.append((score, k))
    if not imgs and not video:
        return None

    def texty(k):
        vals = [r.get(k) for r in rows]
        return sum(isinstance(v, str) and v.strip() and not v.startswith("http") for v in vals) / n
    named = sorted((k for k in cols if re.search(r"name|title|desc|loc|label|site|street|road|route", k, re.I)),
                   key=lambda k: -texty(k))
    name = next((k for k in named if texty(k) > 0.5), None)
    ident = next((k for k in cols if k.lower() in ("objectid", "fid", "id", "cameraid", "camera_id", "camid")), None)
    return {"img": [k for _, k in sorted(imgs, reverse=True)], "video": video, "name": name, "id": ident}


def verify_fields(rows, f):
    """Settle on the image column whose links actually load. Returns the final field map or None."""
    for k in f["img"]:
        urls = [u for u in (_flat(r.get(k)) for r in rows) if u.startswith("http")][:3]
        if urls and sum(probe(u)[0] is True for u in urls) >= max(1, len(urls) - 1):
            return {**f, "img": k, "preview": urls[0]}
    if f["video"]:
        urls = [u for u in (_flat(r.get(f["video"])) for r in rows) if u.startswith("http")][:2]
        if urls and probe(urls[0], video=True)[0]:
            return {**f, "img": None, "preview": ""}
    return None


def socrata_rows(domain, ds, limit):
    """Rows of a Socrata dataset as (attributes, lat, lon), wherever it keeps the location."""
    out = []
    for r in fetch_json(f"https://{domain}/resource/{ds}.json?$limit={limit}", timeout=20 if limit < 1000 else 90):
        lat = lon = None
        for k, v in r.items():
            if isinstance(v, dict) and ("latitude" in v or "coordinates" in v):
                if "coordinates" in v:
                    lon, lat = v["coordinates"][:2]
                else:
                    lat, lon = v.get("latitude"), v.get("longitude")
                break
        if lat is None:
            la = next((r[k] for k in r if k.lower() in ("lat", "latitude", "y")), None)
            lo = next((r[k] for k in r if k.lower() in ("lon", "lng", "long", "longitude", "x")), None)
            lat, lon = la, lo
        try:
            out.append((r, float(lat), float(lon)))
        except (TypeError, ValueError):
            continue
    return out


def arcgis_rows(layer, limit=None):
    if limit:
        q = urllib.parse.urlencode({"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": 4326,
                                    "f": "json", "resultRecordCount": limit})
        feats = fetch_json(f"{layer}/query?{q}", timeout=20).get("features") or []
    else:
        feats = arcgis_features(layer)
    return [(f["attributes"], f["geometry"]["y"], f["geometry"]["x"]) for f in feats
            if (f.get("geometry") or {}).get("x") is not None]


def discovered_rows(d, limit=None):
    if d["kind"] == "socrata":
        return socrata_rows(d["domain"], d["dataset"], limit or 50000)
    return arcgis_rows(d["url"], limit)


def discovered_source(d):
    f = d["fields"]

    def fn(_):
        out = []
        for i, (a, lat, lon) in enumerate(discovered_rows(d)):
            img, video = _flat(a.get(f["img"])) if f["img"] else "", _flat(a.get(f["video"])) if f["video"] else ""
            name = a.get(f["name"]) if f["name"] else None
            if not name and img:  # cities often name the picture after the intersection
                base = urllib.parse.unquote(img.split("?")[0].rsplit("/", 1)[-1])
                name = re.sub(r"\.\w{3,4}$", "", base).replace("_", " ").strip() or None
            out.append(cam(a.get(f["id"]) if f["id"] else i, name or d["title"], lat, lon, img,
                           video if ".m3u8" in video else "", region=d["owner"]))
        return out
    return fn


def register_discovered(d):
    with lock:
        if any(s[0] == d["id"] for s in SOURCES):
            return
        catalog[d["id"]] = load_json(os.path.join(CACHE, d["id"] + ".json"), {"cameras": [], "updated": 0, "error": ""})
        SOURCES.append((d["id"], d["title"], discovered_source(d), f"Found by search · {d['owner']}"))


def on_map_already(pts):
    """True when most of a candidate's points sit on cameras already on the map (another copy of a feed we have)."""
    with lock:
        have = {(round(c[2], 3), round(c[3], 3)) for sid, *_ in SOURCES for c in catalog[sid]["cameras"]}
    hits = sum((round(lat, 3), round(lon, 3)) in have for lat, lon in pts)
    return bool(pts) and hits >= len(pts) * 0.5


def check_candidate(c):
    """Turn a search hit into a found source (dict), False if it isn't a camera feed, or None if it
    couldn't be checked (network trouble), so the next search tries it again. Raises nothing."""
    try:
        if c["kind"] == "socrata":
            layers = [c]
        else:
            info = fetch_json(c["url"] + "?f=json", timeout=20)
            layers = [{**c, "url": f"{c['url']}/{l['id']}", "title": c["title"] if len(info.get("layers", [])) == 1
                       else f"{c['title']}: {l['name']}"}
                      for l in info.get("layers", [])[:6] if l.get("geometryType") == "esriGeometryPoint"]
        for l in layers:
            rows = discovered_rows(l, limit=40)
            fields = pick_fields([a for a, *_ in rows])
            if not fields or on_map_already([(lat, lon) for _, lat, lon in rows]):
                continue
            fields = verify_fields([a for a, *_ in rows], fields)
            if not fields:
                continue
            if l["kind"] == "socrata":
                count = int(fetch_json(f"https://{l['domain']}/resource/{l['dataset']}.json?$select=count(*)")[0]
                            .get("count", len(rows)))
            else:
                count = fetch_json(f"{l['url']}/query?where=1%3D1&returnCountOnly=true&f=json").get("count", len(rows))
            key = l.get("url") or f"{l['domain']}/{l['dataset']}"
            return {**l, "id": "d_" + hashlib.sha1(key.encode()).hexdigest()[:10], "fields": fields,
                    "preview": fields.pop("preview"), "count": count, "added": False}
    except Exception:
        return None
    return False


def search_candidates():
    known, out = known_datasets(), {}

    def query(url):
        try:
            return fetch_json(url)
        except Exception:
            return {}
    arc = [f"https://www.arcgis.com/sharing/rest/search?" + urllib.parse.urlencode(
        {"q": f'{term} type:"Feature Service"', "num": 100, "start": start, "f": "json"})
        for term in DISCOVER_TERMS for start in (1, 101)]
    soc = ["https://api.us.socrata.com/api/catalog/v1?" + urllib.parse.urlencode({"q": term, "only": "datasets", "limit": 100})
           for term in DISCOVER_TERMS]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        arc_res, soc_res = list(pool.map(query, arc)), list(pool.map(query, soc))
    for r in (r for page in arc_res for r in page.get("results", [])):
        url = re.sub(r"/\d+$", "", (r.get("url") or "").rstrip("/"))  # the service, not one layer of it
        m = re.search(r"/rest/services/(\w+)/FeatureServer$", url)
        if m and m.group(1).lower() not in known and CAM_TITLE.search(f"{r.get('title')} {m.group(1)}"):
            out.setdefault(url, {"kind": "arcgis", "url": url, "title": r.get("title") or m.group(1),
                                 "owner": r.get("owner") or "ArcGIS"})
    for r in (r for page in soc_res for r in page.get("results", [])):
        ds, domain = r["resource"]["id"], r["metadata"]["domain"]
        if ds not in known and CAM_TITLE.search(r["resource"]["name"]):
            out.setdefault(f"{domain}/{ds}", {"kind": "socrata", "domain": domain, "dataset": ds,
                                              "title": r["resource"]["name"], "owner": domain})
    return out


def run_discovery():
    with lock:
        if discovery["running"]:
            return
        discovery.update(running=True, checked=0, total=0, status="Searching open-data catalogs…")
    try:
        cands = search_candidates()
        with lock:
            have = {d.get("url") or f"{d['domain']}/{d['dataset']}" for d in discovery["found"]}
            todo = [c for k, c in cands.items() if k not in discovery["seen"] and k not in have]
            discovery.update(total=len(todo), status=f"Testing {len(todo)} datasets for working camera images…")
        with concurrent.futures.ThreadPoolExecutor(10) as pool:
            for key, hit in zip([c.get("url") or f"{c['domain']}/{c['dataset']}" for c in todo],
                                pool.map(check_candidate, todo)):
                with lock:
                    discovery["checked"] += 1
                    if hit is not None:
                        discovery["seen"].add(key)
                    # The same layer is often republished under other owners: same first picture = same feed.
                    if hit and not any(d["id"] == hit["id"] or (hit["preview"] and d["preview"] == hit["preview"])
                                       for d in discovery["found"]):
                        discovery["found"].append(hit)
        found = sum(not d["added"] for d in discovery["found"])
        discovery["status"] = (f"Done: checked {len(todo)} datasets. {found} camera feed"
                               f"{'' if found == 1 else 's'} ready to add." if todo else
                               "Nothing new since the last search.")
    except Exception as e:
        discovery["status"] = f"Search failed: {e}"
    finally:
        discovery["running"] = False
        save_discovery()


def discovery_state():
    with lock:
        return {k: discovery[k] for k in ("running", "checked", "total", "status")} | {
            "found": [{k: d[k] for k in ("id", "title", "owner", "count", "preview", "added")}
                      for d in discovery["found"]]}


def set_discovered(did, add):
    d = next((d for d in discovery["found"] if d["id"] == did), None)
    if not d:
        return False
    d["added"] = add
    if add:
        register_discovered(d)
        threading.Thread(target=refresh, args=(did,), daemon=True).start()
    else:
        with lock:
            SOURCES[:] = [s for s in SOURCES if s[0] != did]
    save_discovery()
    return True


for _d in discovery["found"]:
    if _d["added"]:
        register_discovered(_d)

# Set by the Android app: share_app("text") opens a new text message with the download links,
# share_app("link") offers the same message to any app, share_app("file") sends the APK itself.
share_app = None


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".webmanifest": "application/manifest+json",
        ".js": "text/javascript",
        ".apk": "application/vnd.android.package-archive",
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, *a):
        pass

    def send_head(self):
        # Always send the app's files in full instead of answering 304 from file dates:
        # the Android build stamps every file 1980-02-01, so a date check said "unchanged"
        # forever and phones kept the first version they ever cached. Local, so it's free.
        if "If-Modified-Since" in self.headers:
            del self.headers["If-Modified-Since"]
        return super().send_head()

    def send_header(self, keyword, value):
        if keyword.lower() != "last-modified":
            super().send_header(keyword, value)

    def send_json(self, value, code=200):
        body = json.dumps(value, separators=(",", ":")).encode()
        gz = "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 2048
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/api/cameras":
            with lock:
                shown = {sid: healthy(sid, catalog[sid]["cameras"]) for sid, *_ in SOURCES}
                sources = [{"id": sid, "name": label, "attribution": attr, "count": len(shown[sid][0]),
                            "hidden": shown[sid][1], "updated": catalog[sid].get("updated", 0),
                            "error": catalog[sid].get("error", ""), "loading": sid in refreshing}
                           for sid, label, _, attr in SOURCES]
                cams = {sid: shown[sid][0] for sid, *_ in SOURCES}
            sources.append({"id": "flock", "name": "Flock ALPR cameras (locations)",
                            "attribution": "License-plate readers mapped by OpenStreetMap / DeFlock (deflock.org)",
                            "count": len(flock["pts"]), "updated": flock.get("updated", 0),
                            "error": flock.get("error", ""), "loading": "flock" in refreshing, "lazy": True})
            return self.send_json({"sources": sources, "cameras": cams})
        if url.path == "/api/flock":
            with lock:
                pts, err = flock["pts"], flock.get("error", "")
            if not pts and not err:
                threading.Thread(target=refresh_flock, daemon=True).start()
            return self.send_json({"pts": pts, "updated": flock.get("updated", 0), "error": err,
                                   "loading": "flock" in refreshing})
        if url.path == "/api/report":
            # The app saw this camera's picture or stream fail: recheck it now rather than at the next sweep.
            sid, cid = qs.get("src", [""])[0], qs.get("id", [""])[0]
            c = next((c for c in catalog.get(sid, {}).get("cameras", []) if c[0] == cid), None)
            if c and time.time() - health.get(f"{sid}|{cid}", {}).get("t", 0) > 120:
                threading.Thread(target=check_camera, args=(sid, c), daemon=True).start()
            return self.send_json({"ok": True})
        if url.path == "/api/discover":
            return self.send_json(discovery_state())
        if url.path == "/api/app":
            return self.send_json({"share": share_app is not None})
        if url.path == "/api/status":
            with lock:
                return self.send_json({"loading": sorted(refreshing)})
        if url.path == "/api/geocode":
            try:
                return self.send_json(geocode(qs.get("q", [""])[0]))
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
        if url.path == "/api/state":
            return self.send_json(load_json(STATE_FILE, {"favorites": [], "walls": []}))
        if url.path == "/api/settings":
            return self.send_json({"ny511_key": bool(settings.get("ny511_key"))})
        if url.path.startswith("/snap/sg/"):
            try:
                target = sg_images()["images"][url.path.rsplit("/", 1)[1]]
            except Exception:
                self.send_error(404)
                return
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if url.path == "/snap/tx":
            try:
                img = tx_snapshot(qs["d"][0], qs["id"][0])
            except Exception:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(img)
            return
        if url.path == "/CamAtlas.apk":
            # Served from DATA (not the source repo's android/dist, which an installed copy
            # doesn't have alongside it, and not static/ — the APK is 40MB+ and has no
            # business in the tracked web-app assets). Drop a build there to serve it:
            #   copy android\dist\CamAtlas.apk "%USERPROFILE%\CamAtlas\CamAtlas.apk"
            apk = os.path.join(DATA, "CamAtlas.apk")
            if not os.path.isfile(apk):
                self.send_error(404, "No APK here yet — copy android/dist/CamAtlas.apk to "
                                      + apk)
                return
            size = os.path.getsize(apk)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="CamAtlas.apk"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(apk, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
            return
        if url.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            body = self.read_body()
            save_json(STATE_FILE, {"favorites": body.get("favorites", [])[:2000], "walls": body.get("walls", [])[:200]})
            return self.send_json({"ok": True})
        if path == "/api/settings":
            body = self.read_body()
            if "ny511_key" in body:
                settings["ny511_key"] = str(body["ny511_key"]).strip()
                save_json(SETTINGS_FILE, settings)
                catalog["ny511"]["error"] = ""
                threading.Thread(target=refresh, args=("ny511",), daemon=True).start()
            return self.send_json({"ok": True})
        if path == "/api/refresh":
            refresh_stale(force=True)
            return self.send_json({"ok": True})
        if path == "/api/discover":
            threading.Thread(target=run_discovery, daemon=True).start()
            return self.send_json({"ok": True})
        if path in ("/api/discover/add", "/api/discover/remove"):
            ok = set_discovered(self.read_body().get("id", ""), path.endswith("add"))
            return self.send_json({"ok": ok}, 200 if ok else 404)
        if path == "/api/share-app":
            if share_app is None:
                return self.send_json({"error": "Sharing works in the Android app"}, 404)
            try:
                how = self.read_body().get("how")
                share_app(how if how in ("text", "file") else "link")
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)
            return self.send_json({"ok": True})
        self.send_error(404)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def make_server(port=PORT, host="127.0.0.1"):
    return http.server.ThreadingHTTPServer((host, port), Handler)


# ---- camera health: find cameras whose picture or stream is dead, and hide them -------------
# Agencies leave dead cameras in their lists (404 snapshots, streams that never answer, "camera
# unavailable" placeholder pictures). A slow background sweep probes every camera with tiny
# requests (the first 2 KB of the image, the stream's playlist) and /api/cameras leaves out what
# failed twice in a row. A camera whose stream is dead but whose picture works stays, as a still.
HEALTH_FILE = os.path.join(DATA, "health.json")
# "source|id" -> {"n": image fails in a row, "m": video fails, "z": image size, "t": checked}. A fresh
# install starts from health_seed.json shipped beside this file, so dead cameras are hidden from the
# first launch instead of after the first multi-hour sweep.
health = load_json(HEALTH_FILE, None) or load_json(os.path.join(ROOT, "health_seed.json"), {})
SWEEP_RATE = float(os.environ.get("CAMATLAS_SWEEP_RATE", "6"))  # probes per second; 0 turns the sweep off
RECHECK_S = 12 * 3600
DEAD_AFTER = 2
_health_dirty = threading.Event()
_recent = []  # last probe outcomes, to notice when the network itself is down


def probe(url, video=False):
    """True / False if the image (or stream) answered properly, None if we couldn't tell. Also the
    image's full size in bytes when the server says, which exposes shared placeholder pictures."""
    if url.startswith("/"):
        url = f"http://127.0.0.1:{PORT}{url}"
    if not url.startswith(("http://", "https://")):
        return False, None  # not a usable link at all
    try:
        req = urllib.request.Request(url.replace(" ", "%20"), headers={"User-Agent": UA, "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=25) as r:
            head, ct = r.read(2048), r.headers.get("Content-Type", "").lower()
            rng = r.headers.get("Content-Range", "")
            size = int(rng.rsplit("/", 1)[1]) if "/" in rng and rng.rsplit("/", 1)[1].isdigit() else \
                int(r.headers["Content-Length"]) if r.status == 200 and r.headers.get("Content-Length", "").isdigit() else None
    except urllib.error.HTTPError as e:
        return (None if e.code in (429, 503) else False), None
    except (urllib.error.URLError, OSError, ValueError):
        return False, None
    if video:
        return b"#EXTM3U" in head or ct.startswith("video"), None
    is_img = ct.startswith("image") or head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n"
    return is_img, (size if is_img else None)


def check_camera(sid, c):
    key, now = f"{sid}|{c[0]}", time.time()
    h = dict(health.get(key, {}))
    results = []
    if c[4]:
        ok, size = probe(c[4])
        results.append(ok)
        if ok is not None:
            h["n"], h["z"] = (0, size) if ok else (h.get("n", 0) + 1, None)
    if c[5]:
        ok, _ = probe(c[5], video=True)
        results.append(ok)
        if ok is not None:
            h["m"] = 0 if ok else h.get("m", 0) + 1
    with lock:
        _recent.extend(r is not False for r in results)
        del _recent[:-60]
        network_down = len(_recent) >= 40 and sum(_recent) < 4
    if network_down:  # almost everything failing at once means our connection, not the cameras
        return False
    h["t"] = now
    health[key] = h
    _health_dirty.set()
    return True


# Sources whose image hosts turn scripts away with a "temporarily unavailable" page while serving
# browsers fine, so a probe from here would wrongly hide every camera. Their feeds are trusted as-is.
NO_PROBE = {"nsw"}


def sweeper():
    """Works through every camera, never-checked ones first, then each again every RECHECK_S."""
    if SWEEP_RATE <= 0:
        return
    time.sleep(120)  # let the camera lists load first
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        while True:
            now = time.time()
            with lock:
                due = [(health.get(f"{sid}|{c[0]}", {}).get("t", 0), sid, c)
                       for sid, *_ in SOURCES if sid not in NO_PROBE for c in catalog[sid]["cameras"]
                       if now - health.get(f"{sid}|{c[0]}", {}).get("t", 0) > RECHECK_S]
            if not due:
                time.sleep(600)
                continue
            due.sort(key=lambda d: d[0])
            for i, (_, sid, c) in enumerate(due[:2000]):
                pool.submit(check_camera, sid, c)
                time.sleep(1 / SWEEP_RATE)
                if i % 200 == 199 and len(_recent) >= 40 and sum(_recent[-40:]) < 4:
                    time.sleep(300)  # offline: wait instead of burning through the list


def health_saver():
    while True:
        _health_dirty.wait()
        time.sleep(60)
        _health_dirty.clear()
        with lock:
            snapshot = dict(health)
        save_json(HEALTH_FILE, snapshot)


def healthy(sid, cams):
    """The cameras worth showing, plus how many were hidden as broken."""
    sizes = collections.Counter(h["z"] for c in cams if (h := health.get(f"{sid}|{c[0]}")) and h.get("z"))
    placeholder = {z for z, n in sizes.items() if n >= max(5, len(cams) * 0.03)}
    out, hidden = [], 0
    for c in cams:
        h = health.get(f"{sid}|{c[0]}")
        if not h:
            out.append(c)
            continue
        img_dead = c[4] and (h.get("n", 0) >= DEAD_AFTER or h.get("z") in placeholder)
        vid_dead = c[5] and h.get("m", 0) >= DEAD_AFTER
        img, video = ("" if img_dead else c[4]), ("" if vid_dead else c[5])
        if not img and not video:
            hidden += 1
            continue
        out.append(c if (img, video) == (c[4], c[5]) else c[:4] + [img, video] + c[6:])
    return out, hidden


def write_health_seed(path):
    """Just the broken cameras from this machine's sweep (placeholders resolved to dead), for builds to
    ship as health_seed.json. Small: healthy cameras need no entry."""
    seed = {}
    for sid, *_ in SOURCES:
        cams = catalog[sid]["cameras"]
        shown = {c[0]: c for c in healthy(sid, cams)[0]}
        for c in cams:
            s = shown.get(c[0])
            img_dead, vid_dead = bool(c[4]) and (s is None or not s[4]), bool(c[5]) and (s is None or not s[5])
            if img_dead or vid_dead:
                seed[f"{sid}|{c[0]}"] = {"n": DEAD_AFTER if img_dead else 0, "m": DEAD_AFTER if vid_dead else 0,
                                         "t": health.get(f"{sid}|{c[0]}", {}).get("t", 0)}
    save_json(path, seed)
    return len(seed)


def start_background():
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=sweeper, daemon=True).start()
    threading.Thread(target=health_saver, daemon=True).start()


def lan_ips():
    """Best-effort local network addresses, so a phone on the same Wi-Fi knows where to connect."""
    ips = set()
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


if __name__ == "__main__":
    # --lan serves on all interfaces so another device (e.g. an Android phone) on the same
    # network can reach it; default stays localhost-only.
    host = "0.0.0.0" if "--lan" in sys.argv else "127.0.0.1"
    start_background()
    if host == "0.0.0.0":
        print(f"CamAtlas on http://127.0.0.1:{PORT}  (this PC)")
        for ip in lan_ips():
            print(f"            http://{ip}:{PORT}  (open this on your phone, same Wi-Fi)")
        if not lan_ips():
            print("            (couldn't detect a LAN IP — check your network connection)")
    else:
        print(f"CamAtlas on http://127.0.0.1:{PORT}")
    make_server(host=host).serve_forever()
