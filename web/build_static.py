"""Builds the hosted CamAtlas (web/site/) that iPhones and any browser can open, no server needed.

Runs the same source code as the app (server.py) to download every camera list, then writes
them as plain files next to a copy of the page, marked so app.js answers its /api/ calls from
those files (see STATIC in app.js). GitHub Pages serves the result; .github/workflows/pages.yml
rebuilds it every few hours so the lists stay current.

    python web/build_static.py                 download everything fresh -> web/site/
    python web/build_static.py --data DIR      reuse (and refresh only stale) caches in DIR

Cameras whose pictures only come through the app's own server (Singapore, Texas: /snap/...)
are left out, because a static site has nothing to answer those links.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def https_hosts(server):
    """The hosted page is https, and browsers (iPhone Safari above all) won't load http pictures or
    streams into it. For each host that only got http links, try one of them over https."""
    import urllib.parse
    import urllib.request
    samples = {}
    for sid, *_ in server.SOURCES:
        for c in server.catalog[sid]["cameras"]:
            for u in (c[4], c[5]):
                if u.startswith("http://"):
                    picks = samples.setdefault(urllib.parse.urlsplit(u).netloc, [])
                    if len(picks) < 3:  # one dead picture shouldn't condemn the whole host
                        picks.append(u)
    secure = set()
    for host, picks in samples.items():
        for u in picks:
            try:
                req = urllib.request.Request("https://" + u[7:].replace(" ", "%20"), headers={"User-Agent": server.UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read(1024)
                secure.add(host)
                break
            except Exception:
                pass
    print(f"http-only hosts: {len(samples)}, also on https: {sorted(secure)}")
    return secure


def upgrade(url, secure):
    """https version of an http link if its host serves https, else "" (it couldn't load anyway)."""
    if not url.startswith("http://"):
        return url
    host = url[7:].split("/", 1)[0]
    return "https://" + url[7:] if host in secure else ""


def content_hash(root):
    """Hash of everything an over-the-air update replaces: server.py and static/. The Android build
    stamps the same hash into the APK (content_version.txt), so the app can tell its own code apart
    from a newer published one. Returns (hex digest, sorted file paths)."""
    import hashlib
    files = [os.path.join(root, "server.py")]
    for d, _, names in os.walk(os.path.join(root, "static")):
        files += [os.path.join(d, n) for n in names]
    files.sort(key=lambda f: os.path.relpath(f, root).replace(os.sep, "/"))
    h = hashlib.sha256()
    for f in files:
        rel = os.path.relpath(f, root).replace(os.sep, "/")
        with open(f, "rb") as fh:
            h.update(rel.encode() + b"\0" + fh.read().replace(b"\r\n", b"\n"))  # same on Windows and Linux
    return h.hexdigest(), files


def write_update(server, out):
    """The Android app's over-the-air update: server.py and the page (static/) as one zip, plus a
    manifest naming its hash. The app checks the manifest at startup and, when the hash is new,
    unpacks the zip into its own storage and runs from it on the next launch (see app.py)."""
    import zipfile
    digest, files = content_hash(ROOT)
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(os.path.join(out, "content.zip"), "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, os.path.relpath(f, ROOT).replace(os.sep, "/"))
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"content": digest, "built": int(__import__("time").time())}, f)
    print("Update bundle", digest[:12], f"({len(files)} files)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "site"))
    ap.add_argument("--data", help="CamAtlas data folder whose cache to start from (default: a fresh temp folder)")
    args = ap.parse_args()

    # server.py reads CAMATLAS_DATA when imported, and the sweep must stay off: no server runs here.
    os.environ["CAMATLAS_DATA"] = args.data or tempfile.mkdtemp(prefix="camatlas-")
    os.environ["CAMATLAS_SWEEP_RATE"] = "0"
    sys.path.insert(0, ROOT)
    import server

    jobs = [threading.Thread(target=server.refresh, args=(sid,)) for sid, *_ in server.SOURCES
            if args.data is None or server.catalog[sid]["updated"] < __import__("time").time() - server.CATALOG_TTL]
    jobs.append(threading.Thread(target=server.refresh_flock))
    for t in jobs:
        t.start()
    for t in jobs:
        t.join()

    secure = https_hosts(server)
    sources, cameras = [], {}
    for sid, label, _, attr in server.SOURCES:
        entry = server.catalog[sid]
        shown, hidden = server.healthy(sid, entry["cameras"])
        cams = []
        for c in shown:
            img = "" if c[4].startswith("/") else c[4]  # /snap/... needs the app's server
            img, video = upgrade(img, secure), upgrade(c[5], secure)
            if img or video:
                cams.append(c[:4] + [img, video] + c[6:])
        cameras[sid] = cams
        sources.append({"id": sid, "name": label, "attribution": attr, "count": len(cams), "hidden": hidden,
                        "updated": entry.get("updated", 0), "error": entry.get("error", "") if not cams else "",
                        "loading": False})
        print(f"{sid:16} {len(cams):6}  {entry.get('error', '')}")
    sources.append({"id": "flock", "name": "Flock ALPR cameras (locations)",
                    "attribution": "License-plate readers mapped by OpenStreetMap / DeFlock (deflock.org)",
                    "count": len(server.flock["pts"]), "updated": server.flock.get("updated", 0),
                    "error": server.flock.get("error", ""), "loading": False, "lazy": True})

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    shutil.copytree(server.STATIC, args.out)
    os.makedirs(os.path.join(args.out, "data"))
    with open(os.path.join(args.out, "data", "cameras.json"), "w", encoding="utf-8") as f:
        json.dump({"sources": sources, "cameras": cameras}, f, separators=(",", ":"))
    with open(os.path.join(args.out, "data", "flock.json"), "w", encoding="utf-8") as f:
        json.dump({"pts": server.flock["pts"], "updated": server.flock.get("updated", 0),
                   "error": server.flock.get("error", ""), "loading": False}, f, separators=(",", ":"))
    index = os.path.join(args.out, "index.html")
    with open(index, encoding="utf-8") as f:
        page = f.read()
    marker = '<script src="vendor/leaflet.min.js"></script>'
    if marker not in page:
        sys.exit("index.html changed: can't find where to mark the page as static")
    page = page.replace(marker, "<script>window.CAMATLAS_STATIC = true; "
                                "document.documentElement.classList.add('hosted');</script>\n" + marker, 1)
    with open(index, "w", encoding="utf-8") as f:
        f.write(page)
    open(os.path.join(args.out, ".nojekyll"), "w").close()  # serve vendor/ and dotfiles as-is
    write_update(server, os.path.join(args.out, "update"))
    total = sum(len(c) for c in cameras.values())
    print(f"Built {args.out}: {total:,} cameras from {sum(1 for c in cameras.values() if c)} sources, "
          f"{len(server.flock['pts']):,} Flock points")


if __name__ == "__main__":
    main()
