# AirGradient Outdoor Air Quality Dashboard

A Dash website that shows live and historical readings from several
AirGradient outdoor monitors, and compares them side by side.

## Features

- Pick which monitors to show, and any date range that has data
  (quick ranges: 24 h, 7 days, 30 days, 90 days, 1 year, all).
- Latest reading per monitor, with its US EPA PM2.5 category and an
  online/offline badge.
- Charts for PM2.5, PM10, CO₂, temperature, humidity, TVOC and NOx:
  - line chart over time, with a zoom slider
  - average per monitor (bar chart)
  - hourly / daily / monthly averages, monitors side by side (bar chart,
    with the WHO 24-hour guideline for PM2.5 and PM10)
  - average by hour of day
  - how closely the monitors agree (correlation heatmap)
- Summary statistics table, readings table, and CSV / Excel downloads.

## How the data flows

```
monitors ──► AirGradient cloud ──► app.py (background threads) ──► SQLite file ──► website
```

- Every `REFRESH_SECONDS` the app fetches the latest reading of every monitor.
- On start-up it downloads each monitor's history from the AirGradient API
  (10 days per request, newest first) and fills in anything missed while
  the app was off. Progress is saved, so restarts continue where they
  stopped.
- Readings are stored in a local SQLite database (`data/airgradient.sqlite`,
  table `measurements`, one row per monitor per reading). The website only
  reads from this file.

## Setup

Requires Python 3.10+.

```bash
pip install -r requirements.txt
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

Edit `.env` and set your own `AIRGRADIENT_API_TOKEN` and `MONITOR_IDS`
(the location IDs of your monitors). Then run:

```bash
python app.py
```

and open http://localhost:8050.

## How the API key is protected

- **Server-side only.** The AirGradient token is read from the server's
  environment (`AIRGRADIENT_API_TOKEN`) by `app.py`, and only Python code on
  the server calls the AirGradient API. The browser never talks to
  AirGradient: it calls this app's own Dash callback endpoint
  (`/_dash-update-component`), which reads from the local database.
- **Nothing secret is sent to the browser.** The page, its layout and all
  callback responses contain neither the token nor the monitors' location
  IDs. The browser refers to monitors by neutral keys (`m1`, `m2`, …) that
  the server maps back to IDs.
- **Not in logs.** AirGradient only accepts the token as a `?token=` URL
  parameter, so error messages are reduced to the error type / HTTP status
  and the HTTP library's URL logging is switched off.
- **Not in git.** `.env` and `data/` are in `.gitignore`. Only
  `.env.example`, with placeholder values, is committed. Never put the real
  token in code, the README or any `NEXT_PUBLIC_*` / `VITE_*`-style public
  variable.
- **If the token leaks** (screenshot, chat, commit): create a new one in the
  AirGradient dashboard and update `.env` / your host's settings. Deleting a
  committed file does not remove it from git history.

## Deploying to a host

Do not upload `.env`. Set the same variables in the host's
environment-variable settings instead (e.g. Render → *Environment*,
Railway → *Variables*, Azure App Service → *Configuration*):

| Variable | Secret? |
|---|---|
| `AIRGRADIENT_API_TOKEN` | **yes** |
| `MONITOR_IDS` | yes (keep private) |
| `HISTORY_START`, `REFRESH_SECONDS`, `DISPLAY_UTC_OFFSET_HOURS`, `DISPLAY_TZ_LABEL`, `DATABASE_PATH` | no |

Start command (Linux hosts):

```bash
gunicorn app:server --workers 1 --threads 8 --bind 0.0.0.0:$PORT
```

- Use **one worker**: each worker starts its own background collector.
- Point `DATABASE_PATH` at a **persistent disk** (for example
  `/var/data/airgradient.sqlite`); many hosts wipe the normal file system on
  every deploy, which would make the app re-download all history.
