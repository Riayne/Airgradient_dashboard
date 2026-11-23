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

## Keeping secrets out of git

`.env` (API token and monitor IDs) and `data/` (the database) are listed in
`.gitignore` and are never committed. Only `.env.example`, with placeholder
values, is in the repository.
