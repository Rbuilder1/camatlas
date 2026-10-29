"""Pre-built map places (springs, gas stations) for the hosted site, so those layers don't depend on
the public Overpass servers, which are often at capacity. Built for every US state.

Reads Geofabrik OpenStreetMap extracts (<state>.osm.pbf, with the <state>.poly boundary beside it),
filters them with osmium-tool, and writes the same 0.25-degree tiles app.js's placesLayer uses:

    places/index.json             {"tile": 0.25, "covered": ["i.j", ...], "built": unix time}
    places/<layer>/<i>.<j>.json   [[osm id, lat, lon, {kept tags}], ...]   (one per covered tile)

Springs the USGS knows about that OpenStreetMap doesn't are added too (see usgs_springs).

A tile counts as covered when a corner or its middle is inside one of the states. With every state
downloaded, only tiles along the Canadian and Mexican borders can come out partial.

    python web/build_places.py --out site/places osm/*.osm.pbf
"""
import argparse
import bisect
import json
import math
import os
import re
import subprocess
import tempfile
import time

TILE = 0.25
# Same selections and kept tags as the layers in static/app.js.
LAYERS = {
    "gas": (lambda t: t.get("amenity") == "fuel",
            re.compile(r"^(name|brand|opening_hours|addr:(housenumber|street|city)|fuel:.*)$")),
    "springs": (lambda t: t.get("natural") == "spring",
                re.compile(r"^(name|drinking_water|seasonal|intermittent|description|access|ele)$")),
}
OSMIUM_FILTER = ["n/natural=spring", "nwr/amenity=fuel"]
OSM_TYPE = {"n": "node", "w": "way", "r": "relation"}


def read_poly(path):
    """Geofabrik .poly: a name line, then rings (a header line, 'lon lat' lines, END), then END.
    A ring whose header starts with '!' is a hole. Returns [(is_hole, [(lon, lat), ...]), ...]."""
    lines = [l.strip() for l in open(path) if l.strip()][1:]  # skip the name
    rings, ring, hole = [], None, False
    for line in lines:
        if line == "END":
            if ring is None:
                break  # end of file
            rings.append((hole, ring))
            ring = None
        elif ring is None:
            ring, hole = [], line.startswith("!")
        else:
            x, y = line.split()[:2]
            ring.append((float(x), float(y)))
    return rings


def grid_inside(rings, step):
    """Which points of the `step`-degree grid are inside the area: {(i, j)} for the point at lat i*step,
    lon j*step. One horizontal scan per grid row, so a big state with a detailed border stays quick."""
    lats = [y for _, r in rings for _, y in r]
    lons = [x for _, r in rings for x, _ in r]
    j0, j1 = math.floor(min(lons) / step), math.floor(max(lons) / step) + 1
    out = set()
    for i in range(math.floor(min(lats) / step), math.floor(max(lats) / step) + 1):
        lat = i * step
        depth = {}
        for hole, ring in rings:
            xs = sorted(x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
                        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]) if (y1 > lat) != (y2 > lat))
            if not xs:
                continue
            for j in range(max(j0, math.floor(xs[0] / step)), min(j1, math.floor(xs[-1] / step) + 1)):
                if (len(xs) - bisect.bisect_right(xs, j * step)) % 2:  # odd crossings to the east = inside
                    depth[j] = depth.get(j, 0) + (-1 if hole else 1)
        out.update((i, j) for j, n in depth.items() if n > 0)
    return out


def centre(geom):
    """A point for any exported geometry: the point itself, else the middle of its bounding box."""
    pts = []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            pts.append(c)
        else:
            for x in c:
                walk(x)
    walk(geom["coordinates"])
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2


def usgs_springs():
    """Springs from the USGS for the whole country: named springs from its place-names list (GNIS) and
    springs/seeps on its water map (NHD, ftype 458). Both are public ArcGIS services. Anything outside
    the covered tiles is dropped when the tiles are written."""
    import urllib.parse
    import urllib.request

    def query(url, where, fields):
        out, offset = [], 0
        while True:
            q = urllib.parse.urlencode({"where": where, "outSR": 4326, "outFields": fields, "orderByFields": "objectid",
                                        "f": "json", "resultOffset": offset, "resultRecordCount": 1000})
            req = urllib.request.Request(f"{url}/query?{q}", headers={"User-Agent": "CamAtlas/1.0 (public camera map)"})
            for attempt in range(4):
                try:
                    d = json.loads(urllib.request.urlopen(req, timeout=120).read())
                    if "error" in d:
                        raise RuntimeError(d["error"])
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(10 * (attempt + 1))
            feats = d.get("features") or []
            out += feats
            offset += len(feats)
            if not feats or not d.get("exceededTransferLimit"):
                return out

    found = {}
    for f in query("https://carto.nationalmap.gov/arcgis/rest/services/geonames/MapServer/7", "gaz_featureclass='Spring'",
                   "objectid,gaz_id,gaz_name"):
        a, g = f["attributes"], f.get("geometry")
        if g:
            found[f"gnis{a['gaz_id']}"] = (g["y"], g["x"], {"name": a.get("gaz_name") or "Spring",
                                                             "description": "Named spring in the USGS place-names list"})
    print("USGS place-names springs:", len(found), flush=True)
    for f in query("https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/0", "ftype=458",
                   "objectid,permanent_identifier,gnis_name"):
        a = {k.lower(): v for k, v in f["attributes"].items()}  # this service's field names are upper case
        g = f.get("geometry")
        if not g:
            continue
        tags = {"description": "Spring or seep on the USGS water map"}
        if a.get("gnis_name"):
            tags["name"] = a["gnis_name"]
        found[f"nhd{a.get('permanent_identifier') or a.get('objectid')}"] = (g["y"], g["x"], tags)
    print("USGS springs in all:", len(found), flush=True)
    return [(oid, lat, lon, tags) for oid, (lat, lon, tags) in found.items()]


def near(a, b, metres):
    dy = math.radians(b[0] - a[0]) * 6371000
    dx = math.radians(b[1] - a[1]) * 6371000 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy) < metres


def add_usgs(springs, extra):
    """Add USGS springs that OpenStreetMap doesn't already have (within 300 m, or the same name within 3 km).
    `springs` is the springs layer's {tile key: {id: [id, lat, lon, tags]}}. Returns how many were added."""
    added = 0
    for oid, lat, lon, tags in extra:
        i, j = math.floor(lat / TILE), math.floor(lon / TILE)
        nearby = [s for di in (-1, 0, 1) for dj in (-1, 0, 1) for s in springs.get(f"{i + di}.{j + dj}", {}).values()]
        name = (tags.get("name") or "").lower()
        if any(near((lat, lon), (s[1], s[2]), 300) or (name and (s[3].get("name") or "").lower() == name and near((lat, lon), (s[1], s[2]), 3000))
               for s in nearby):
            continue
        springs.setdefault(f"{i}.{j}", {})[oid] = [oid, round(lat, 6), round(lon, 6), tags]
        added += 1
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("pbf", nargs="+")
    args = ap.parse_args()

    polys = [read_poly(p[:-len(".osm.pbf")] + ".poly") for p in args.pbf]
    tiles = {name: {} for name in LAYERS}
    with tempfile.TemporaryDirectory() as tmp:
        for pbf in args.pbf:
            small = os.path.join(tmp, "f.osm.pbf")
            seq = os.path.join(tmp, "f.geojsonseq")
            subprocess.run(["osmium", "tags-filter", "--overwrite", "-o", small, pbf] + OSMIUM_FILTER, check=True)
            subprocess.run(["osmium", "export", "--overwrite", "-f", "geojsonseq", "-u", "type_id",
                            "-o", seq, small], check=True)
            for line in open(seq, encoding="utf-8"):
                f = json.loads(line.lstrip("\x1e"))
                t = f.get("properties") or {}
                oid = f.get("id") or t.get("@id") or ""
                oid = OSM_TYPE.get(oid[:1], "") + oid[1:]
                lat, lon = centre(f["geometry"])
                key = f"{math.floor(lat / TILE)}.{math.floor(lon / TILE)}"
                for name, (wanted, keep) in LAYERS.items():
                    if wanted(t):
                        kept = {k: v for k, v in t.items() if keep.match(k)}
                        tiles[name].setdefault(key, {})[oid] = [oid, round(lat, 6), round(lon, 6), kept]

    try:
        print("USGS springs added:", add_usgs(tiles["springs"], usgs_springs()))
    except Exception as e:  # a USGS outage shouldn't stop the weekly build; OSM springs still go out
        print("USGS springs skipped:", e)

    # Covered = a corner or the middle inside one of the downloaded states. A half-tile grid holds both:
    # tile (i, j) has its corners at (2i, 2j)..(2i+2, 2j+2) and its middle at (2i+1, 2j+1).
    pts = set()
    for rings in polys:
        pts |= grid_inside(rings, TILE / 2)
    covered = set()
    for a, b in pts:
        for i in {(a - 1) // 2, a // 2}:
            for j in {(b - 1) // 2, b // 2}:
                if (a - 2 * i, b - 2 * j) in ((0, 0), (0, 2), (2, 0), (2, 2), (1, 1)):
                    covered.add(f"{i}.{j}")

    for name in LAYERS:
        os.makedirs(os.path.join(args.out, name), exist_ok=True)
        for key in covered:
            with open(os.path.join(args.out, name, key + ".json"), "w", encoding="utf-8") as fh:
                json.dump(list(tiles[name].get(key, {}).values()), fh, separators=(",", ":"))
    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as fh:
        json.dump({"tile": TILE, "covered": sorted(covered), "built": int(time.time())}, fh, separators=(",", ":"))
    print(f"places: {len(covered)} covered tiles;",
          ", ".join(f"{sum(len(v) for k, v in tiles[n].items() if k in covered)} {n}" for n in LAYERS))


if __name__ == "__main__":
    main()
