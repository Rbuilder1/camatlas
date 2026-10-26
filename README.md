# CamWorld

*(formerly CamAtlas; the links below are unchanged)*

A map of live public traffic, weather, river and wildfire cameras: about 50,000 cameras from
transportation departments and other public agencies, with live video where they offer it,
favorites, and a Monitor wall of many cameras at once.

## iPhone (and any browser)

Open **https://rbuilder1.github.io/camatlas/** in Safari, tap **Share**, then **Add to Home Screen**.
It then opens full-screen from its own icon like any other app. Camera lists refresh every 6 hours.

### iPhone app (SideStore / AltStore)

Prefer a real app over the home-screen web app? CamWorld is also a native iPhone app, shared through
a **SideStore/AltStore source** (Apple only allows App Store installs otherwise):

1. Set up **SideStore** (sidestore.io) or **AltStore** (altstore.io) on your iPhone once, following
   their guide (needs a computer for the first setup).
2. In SideStore/AltStore, go to **Sources → +** and add:
   `https://github.com/Rbuilder1/camatlas/releases/download/ios-latest/altstore-source.json`
3. Open **CamWorld** in that source and tap **Install**. New builds show up there as updates.
4. First time only: Settings → Privacy & Security → turn on **Developer Mode**.

With a free Apple ID, SideStore/AltStore re-sign the app every 7 days for you. Everyone else: the web
app above is simpler and needs no setup.

## Android

1. On your phone, download **[CamAtlas.apk](https://github.com/Rbuilder1/camatlas/releases/latest/download/CamAtlas.apk)**.
2. Open the downloaded file. If Android asks, allow your browser (or Files app) to install apps.
3. Tap **Install**, then open CamAtlas.

Android 7 or newer. The Android app also has **Find more cameras**, which searches public
open-data catalogs for camera feeds to add. Updates are posted under
[Releases](https://github.com/Rbuilder1/camatlas/releases); install a newer APK over the old one.

## Sharing it

On iPhone, open **Sources** and tap **Share CamAtlas**. On Android, use the **⋮** menu:
**Text a download link** or **Send the app file**.

## How the web version is built

`web/build_static.py` runs the same camera-gathering code as the app (`server.py`) and writes the
lists as files next to the page in `static/`. The workflow in `.github/workflows/pages.yml` runs it
on a schedule and publishes the result to GitHub Pages. `health_seed.json` lists cameras known to
be broken, so they stay hidden.

Each camera's image belongs to the agency credited for it in the app's Sources panel.
