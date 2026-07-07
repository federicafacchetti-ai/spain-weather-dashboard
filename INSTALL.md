# AEMET Weather Alert Dashboard — Setup

One-time setup on your Mac. After this, the dashboard updates itself every morning at 08:00 Madrid time.

## What's in this folder

- `aemet_fetch.py` — the daily fetcher. Reads AEMET Open Data API, filters to your 155 cities, updates data files, regenerates `dashboard.html`.
- `dashboard.html` — the viewer (3 tabs: Today / Historical Repository / Weekly Summary). Regenerated automatically each run.
- `cities.json` — the 155-city list (do not edit; regenerate from `SPAIN City _ Weather tracker.xlsx` if the list changes).
- `.aemet_key` — your API key (already saved).
- `data/` — persistent files: `today.json`, `history.jsonl`, `weekly.json`, `last_run.log`.
- `com.glovo.aemet.plist` — macOS launchd config for the 08:00 daily job.

## Step 1 — First run (seed the history)

Open Terminal and run:

```bash
cd "/Users/federica.facchetti/Desktop/Fede Claude/Weather/aemet_dashboard"
python3 aemet_fetch.py --seed
```

You should see:
- "Cities loaded: 155 across 45 provinces"
- "Alerts parsed: N"
- "History appended: N new rows"
- "Dashboard regenerated."

Then double-click `dashboard.html` — it opens in your browser with three tabs.

## Step 2 — Wire up the 08:00 daily refresh

```bash
cd "/Users/federica.facchetti/Desktop/Fede Claude/Weather/aemet_dashboard"

# Replace the placeholder path in the plist with the real one
FOLDER="$(pwd)"
sed "s|REPLACE_ME|$FOLDER|g" com.glovo.aemet.plist > ~/Library/LaunchAgents/com.glovo.aemet.plist

# Load it — this schedules the job
launchctl load ~/Library/LaunchAgents/com.glovo.aemet.plist

# Verify it's registered
launchctl list | grep aemet
```

The dashboard will now refresh every morning at 08:00. Look at `data/launchd.log` and `data/last_run.log` afterwards to confirm.

## Uninstalling / pausing the schedule

```bash
launchctl unload ~/Library/LaunchAgents/com.glovo.aemet.plist
```

To resume, `launchctl load` it again.

## Troubleshooting

- **"no API key"**: check `.aemet_key` file is present and not empty.
- **HTTP 401 from AEMET**: key expired or wrong. Get a new one at https://opendata.aemet.es/centrodedescargas/altaUsuario and overwrite `.aemet_key`.
- **Job doesn't run at 08:00**: your Mac was asleep. launchd will run it as soon as the Mac wakes.
- **Dashboard shows old date**: means the fetcher hasn't run since. Run it manually with `python3 aemet_fetch.py`.

## Manual refresh anytime

```bash
python3 aemet_fetch.py
```

Reopen `dashboard.html` to see the latest.
