# CamAtlas

A map of live public traffic, weather, river and wildfire cameras: about 50,000 cameras from
transportation departments and other public agencies, with live video where they offer it,
favorites, and a Monitor wall of many cameras at once.

## iPhone (and any browser)

Open **https://rbuilder1.github.io/camatlas/** in Safari, tap **Share**, then **Add to Home Screen**.
It then opens full-screen from its own icon like any other app. Camera lists refresh every 6 hours.

### iPhone app file (advanced, optional)

There's also a native iPhone build: **[CamAtlas-unsigned.ipa](https://github.com/Rbuilder1/camatlas/releases/download/ios-latest/CamAtlas-unsigned.ipa)**.
Apple only installs signed apps, so without a paid developer account it has to be signed with your own
Apple ID by a sideloading tool:

1. Install **SideStore** (sidestore.io) or **AltStore** (altstore.io) on your iPhone, following their setup
   guide (needs a computer once).
2. Download `CamAtlas-unsigned.ipa` on the iPhone, open it with SideStore/AltStore, and sign in with your Apple ID.
3. On the iPhone: Settings → General → VPN & Device Management → trust your Apple ID; also turn on
   Settings → Privacy & Security → Developer Mode.

Free Apple IDs: the app must be refreshed every 7 days (SideStore/AltStore do it for you) and only 3
sideloaded apps at a time. For everyone else, the web app above is simpler.

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
