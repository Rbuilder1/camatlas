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

    sources, cameras = [], {}
    for sid, label, _, attr in server.SOURCES:
        entry = server.catalog[sid]
        shown, hidden = server.healthy(sid, entry["cameras"])
        cams = []
        for c in shown:
            img = "" if c[4].startswith("/") else c[4]  # /snap/... needs the app's server
            if img or c[5]:
                cams.append(c[:4] + [img] + c[5:])
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
    page = page.replace(marker, "<script>window.CAMATLAS_STATIC = true;</script>\n" + marker, 1)
    with open(index, "w", encoding="utf-8") as f:
        f.write(page)
    open(os.path.join(args.out, ".nojekyll"), "w").close()  # serve vendor/ and dotfiles as-is
    total = sum(len(c) for c in cameras.values())
    print(f"Built {args.out}: {total:,} cameras from {sum(1 for c in cameras.values() if c)} sources, "
          f"{len(server.flock['pts']):,} Flock points")


if __name__ == "__main__":
    main()
