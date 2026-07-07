# Spain Weather Alert Dashboard

Live dashboard of AEMET Meteoalerta alerts (Yellow / Orange / Red) for 155 tracked cities.

**Live URL:** _will appear here once GitHub Pages is enabled_

## What's in this repo

- `index.html` — the dashboard, self-contained (all data embedded)
- `aemet_fetch.py` — daily fetcher, calls the AEMET Open Data API and regenerates `index.html`
- `server.py` — small local server used when running the dashboard on your Mac (adds a working "Refresh now" button)
- `cities.json` — the 155-city × 45-province mapping
- `Refresh Dashboard.command` — double-click to run a manual refresh (macOS)
- `Start Dashboard.command` — double-click to start the local server + open the dashboard with the button
- `com.glovo.aemet.plist` — macOS launchd config for automatic 08:00 daily refresh

## Not in this repo (intentionally)

- `.aemet_key` — API key, kept locally
- `data/` — accumulated history, weekly rollup, red alerts CSV; local only

## How the daily refresh works

- Locally on the operator's Mac, launchd runs `aemet_fetch.py` at 08:00 Madrid time
- The script fetches AEMET's public alert bundle, filters to Yellow / Orange / Red for the 155 tracked cities
- Regenerates `index.html` with the fresh data embedded
- The operator commits + pushes; GitHub Pages serves the fresh version

## Three tabs

- **Today** — one card per city, coloured pills for active alerts (max 3 per city: Yellow / Orange / Red)
- **Historical Repository** — every alert ever captured, filterable by day / week / level / event type / city
- **Weekly Summary** — hours per level per city per ISO week, plus a "no alert hrs" column
