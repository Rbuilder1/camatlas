"""Pre-built map places (springs, gas stations) for the hosted site, so those layers don't depend on
the public Overpass servers, which are often at capacity.

Reads Geofabrik OpenStreetMap extracts (<state>.osm.pbf, with the <state>.poly boundary beside it),
filters them with osmium-tool, and writes the same 0.25-degree tiles app.js's placesLayer uses:

    places/index.json             {"tile": 0.25, "covered": ["i.j", ...], "built": unix time}
    places/<layer>/<i>.<j>.json   [[osm id, lat, lon, {kept tags}], ...]   (one per covered tile)

A tile counts as covered only when all four corners are inside the states, so border tiles (half in a
state that wasn't downloaded) keep asking Overpass instead of showing up half empty.

    python web/build_places.py --out site/places osm/new-york.osm.pbf osm/new-jersey.osm.pbf ...
"""
import argparse
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


def in_ring(lon, lat, ring):
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def inside(lon, lat, rings):
    return (any(in_ring(lon, lat, r) for h, r in rings if not h)
            and not any(in_ring(lon, lat, r) for h, r in rings if h))


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


def usgs_springs(polys):
    """Springs only the USGS has: named springs from its place-names list (GNIS) and springs/seeps on
    its water map (NHD, ftype 458), inside the downloaded states. Both are public ArcGIS services."""
    import urllib.parse
    import urllib.request

    def query(url, where, box):
        out, offset = [], 0
        while True:
            q = urllib.parse.urlencode({"where": where, "geometry": ",".join(map(str, box)), "geometryType": "esriGeometryEnvelope",
                                        "inSR": 4326, "outSR": 4326, "outFields": "*", "f": "json",
                                        "resultOffset": offset, "resultRecordCount": 1000})
            req = urllib.request.Request(f"{url}/query?{q}", headers={"User-Agent": "CamAtlas/1.0 (public camera map)"})
            d = json.loads(urllib.request.urlopen(req, timeout=120).read())
            feats = d.get("features") or []
            out += feats
            offset += len(feats)
            if not feats or not d.get("exceededTransferLimit"):
                return out

    found = []
    for rings in polys:
        lons = [x for _, r in rings for x, _ in r]
        lats = [y for _, r in rings for _, y in r]
        box = (min(lons), min(lats), max(lons), max(lats))
        for f in query("https://carto.nationalmap.gov/arcgis/rest/services/geonames/MapServer/7", "gaz_featureclass='Spring'", box):
            a, g = f["attributes"], f["geometry"]
            found.append((f"gnis{a['gaz_id']}", g["y"], g["x"], {"name": a.get("gaz_name") or "Spring",
                                                                 "description": "Named spring in the USGS place-names list"}))
        for f in query("https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/0", "ftype=458", box):
            a = {k.lower(): v for k, v in f["attributes"].items()}  # this service's field names are upper case
            g = f["geometry"]
            tags = {"description": "Spring or seep on the USGS water map"}
            if a.get("gnis_name"):
                tags["name"] = a["gnis_name"]
            found.append((f"nhd{a.get('permanent_identifier') or a.get('objectid')}", g["y"], g["x"], tags))
    # Keep the ones inside the states (the boxes overlap neighbours), once each.
    seen, out = set(), []
    for oid, lat, lon, tags in found:
        if oid not in seen and any(inside(lon, lat, r) for r in polys):
            seen.add(oid)
            out.append((oid, lat, lon, tags))
    return out


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
        print("USGS springs added:", add_usgs(tiles["springs"], usgs_springs(polys)))
    except Exception as e:  # a USGS outage shouldn't stop the weekly build; OSM springs still go out
        print("USGS springs skipped:", e)

    # Covered = every corner inside one of the downloaded states.
    covered = set()
    for rings in polys:
        lons = [x for _, r in rings for x, _ in r]
        lats = [y for _, r in rings for _, y in r]
        for i in range(math.floor(min(lats) / TILE), math.floor(max(lats) / TILE) + 1):
            for j in range(math.floor(min(lons) / TILE), math.floor(max(lons) / TILE) + 1):
                corners = [(j * TILE, i * TILE), ((j + 1) * TILE, i * TILE), (j * TILE, (i + 1) * TILE), ((j + 1) * TILE, (i + 1) * TILE)]
                if all(any(inside(x, y, r) for r in polys) for x, y in corners):
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
