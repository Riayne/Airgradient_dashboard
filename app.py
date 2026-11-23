"""
AirGradient outdoor air-quality dashboard.

Collects data from several AirGradient outdoor monitors into a local SQLite
database (live readings every REFRESH_SECONDS, plus a one-off backfill of
each monitor's full history), and serves a Dash website where people pick
which monitors to view, any date range that has data, and compare the
monitors in line and bar charts.
"""

import os
import time
import sqlite3
import logging
import threading
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import (
    Dash, html, dcc, Input, Output, State, ctx, dash_table, no_update
)
from dotenv import load_dotenv


# ============================================================
# 1. CONFIGURATION
# ============================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("airgradient")

API_BASE = os.getenv(
    "AIRGRADIENT_BASE_URL",
    "https://api.airgradient.com/public/api/v1",
).rstrip("/")
API_TOKEN = os.getenv("AIRGRADIENT_API_TOKEN", "").strip()
AUTH_MODE = os.getenv("AIRGRADIENT_AUTH_MODE", "query").lower()
AUTH_HEADER = os.getenv("AIRGRADIENT_AUTH_HEADER", "X-API-Key")

# The monitors shown on the site, in display order. Order fixes each
# monitor's colour, so a monitor keeps its colour whatever is selected.
# Set in .env (never in code), e.g. MONITOR_IDS=111111,222222
MONITOR_IDS = [
    int(x) for x in os.getenv("MONITOR_IDS", "").split(",") if x.strip()
]
if not MONITOR_IDS:
    raise SystemExit(
        "MONITOR_IDS is not set. Copy .env.example to .env and fill in "
        "your AirGradient location IDs."
    )

REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "60"))

# How far back the history backfill looks.
HISTORY_START = datetime.fromisoformat(
    os.getenv("HISTORY_START", "2024-01-01")
).replace(tzinfo=timezone.utc)

# Times are shown in local time. Pakistan has no daylight saving, so a
# fixed offset is exact and needs no timezone database.
UTC_OFFSET_HOURS = float(os.getenv("DISPLAY_UTC_OFFSET_HOURS", "5"))
TZ_LABEL = os.getenv("DISPLAY_TZ_LABEL", "PKT")
OFFSET = timedelta(hours=UTC_OFFSET_HOURS)
SQL_OFFSET = f"{UTC_OFFSET_HOURS * 60:+.0f} minutes"

DB_PATH = Path(os.getenv("DATABASE_PATH", "data/airgradient.sqlite"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

STALE_MINUTES = 30          # a monitor silent this long is shown as offline
BACKFILL_WINDOW_DAYS = 10   # the API's maximum range per history request


# ============================================================
# 2. PARAMETERS
# ============================================================

# Column name -> how it is shown and which API fields feed it. The first
# field that has a value wins, so AirGradient's corrected values are used
# when present.
METRICS = {
    "pm25": dict(label="PM2.5", unit="µg/m³", dp=1, who=15,
                 keys=["pm02_corrected", "pm02"]),
    "pm10": dict(label="PM10", unit="µg/m³", dp=1, who=45,
                 keys=["pm10_corrected", "pm10"]),
    "co2": dict(label="CO₂", unit="ppm", dp=0,
                keys=["rco2_corrected", "rco2"]),
    "temp": dict(label="Temperature", unit="°C", dp=1,
                 keys=["atmp_corrected", "atmp"]),
    "rh": dict(label="Humidity", unit="%", dp=0,
               keys=["rhum_corrected", "rhum"]),
    "tvoc": dict(label="TVOC index", unit="index", dp=0,
                 keys=["tvocIndex"]),
    "nox": dict(label="NOx index", unit="index", dp=0,
                keys=["noxIndex"]),
}
COLS = list(METRICS)

# US EPA PM2.5 categories (2024 breakpoints, µg/m³).
AQI_BANDS = [
    (9.0, "Good", "#00e400"),
    (35.4, "Moderate", "#ffff00"),
    (55.4, "Unhealthy for sensitive groups", "#ff7e00"),
    (125.4, "Unhealthy", "#ff0000"),
    (225.4, "Very unhealthy", "#8f3f97"),
    (float("inf"), "Hazardous", "#7e0023"),
]


# ============================================================
# 3. VISUAL STYLE
# ============================================================

FONT = "Inter, 'Segoe UI', system-ui, -apple-system, sans-serif"
SURFACE = "#ffffff"
TEXT_1 = "#0b0b0b"
TEXT_2 = "#52514e"
MUTED = "#8a8984"
GRID = "#ebeae6"

# Categorical palette, fixed order (validated reference palette).
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
           "#e87ba4", "#008300", "#6250d6", "#e34948"]
SYMBOLS = ["circle", "square", "diamond", "triangle-up",
           "triangle-down", "cross", "x", "star"]
COLOR = {m: PALETTE[i % len(PALETTE)] for i, m in enumerate(MONITOR_IDS)}
SYMBOL = {m: SYMBOLS[i % len(SYMBOLS)] for i, m in enumerate(MONITOR_IDS)}

# Diverging scale for correlations: orange (-1), grey (0), blue (+1).
DIVERGING = [[0, "#eb6834"], [0.5, "#e8e7e3"], [1, "#2a78d6"]]


# ============================================================
# 4. DATABASE
# ============================================================

def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_database():
    metric_cols = ", ".join(f"{c} REAL" for c in COLS)
    with closing(connect()) as con, con:
        con.executescript(f"""
            CREATE TABLE IF NOT EXISTS measurements (
                location_id INTEGER NOT NULL,
                ts TEXT NOT NULL,            -- UTC, 'YYYY-MM-DD HH:MM:SS'
                {metric_cols},
                PRIMARY KEY (location_id, ts)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS monitors (
                location_id INTEGER PRIMARY KEY,
                name TEXT,
                model TEXT,
                last_seen TEXT
            );

            -- Progress of the history backfill for each monitor.
            CREATE TABLE IF NOT EXISTS backfill (
                location_id INTEGER PRIMARY KEY,
                oldest_done TEXT NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0
            );
        """)


def query(sql, params=()):
    with closing(connect()) as con:
        return pd.read_sql_query(sql, con, params=list(params))


def utc_str(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_utc(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


def save_rows(rows):
    if not rows:
        return
    placeholders = ",".join("?" * (2 + len(COLS)))
    with closing(connect()) as con, con:
        con.executemany(
            f"INSERT OR REPLACE INTO measurements "
            f"(location_id, ts, {', '.join(COLS)}) "
            f"VALUES ({placeholders})",
            rows,
        )


# ============================================================
# 5. AIRGRADIENT API
# ============================================================

def api_get(path, **params):
    """GET an API path, retrying on errors. Never puts the token in logs."""
    if not API_TOKEN:
        raise RuntimeError("AIRGRADIENT_API_TOKEN is missing from .env")

    headers = {"Accept": "application/json"}
    if AUTH_MODE == "header":
        headers[AUTH_HEADER] = API_TOKEN
    else:
        params["token"] = API_TOKEN

    problem = "unknown error"
    for attempt in range(4):
        try:
            response = requests.get(
                f"{API_BASE}{path}", params=params,
                headers=headers, timeout=60,
            )
            if response.status_code == 429:
                problem = "rate limited (429)"
                time.sleep(30 * (attempt + 1))
                continue
            if response.status_code >= 400:
                problem = f"HTTP {response.status_code}"
                if response.status_code < 500:
                    break           # a client error will not fix itself
                time.sleep(5 * (attempt + 1))
                continue
            return response.json()
        except requests.RequestException as error:
            problem = type(error).__name__
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"AirGradient API request failed: {problem}")


def api_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def first_number(measure, keys):
    for key in keys:
        value = measure.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def to_row(location_id, measure):
    stamp = measure.get("timestamp")
    if not stamp:
        return None
    try:
        ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        int(location_id),
        utc_str(ts),
        *[first_number(measure, METRICS[c]["keys"]) for c in COLS],
    )


def fetch_past(location_id, start, end):
    data = api_get(
        f"/locations/{location_id}/measures/past",
        **{"from": api_time(start), "to": api_time(end)},
    )
    if not isinstance(data, list):
        return []
    return [r for r in (to_row(location_id, m) for m in data) if r]


# ============================================================
# 6. BACKGROUND COLLECTION
# ============================================================

STATUS = {"last_poll": None, "last_error": None}


def poll_current():
    """Store the latest reading of every monitor (one API request)."""
    data = api_get("/locations/measures/current")
    rows, monitors = [], []
    for measure in data if isinstance(data, list) else []:
        location_id = measure.get("locationId")
        if location_id not in MONITOR_IDS:
            continue
        row = to_row(location_id, measure)
        if row:
            rows.append(row)
        monitors.append((
            int(location_id),
            measure.get("locationName") or f"Monitor {location_id}",
            measure.get("model"),
            row[1] if row else None,
        ))
    save_rows(rows)
    with closing(connect()) as con, con:
        con.executemany("""
            INSERT INTO monitors (location_id, name, model, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(location_id) DO UPDATE SET
                name = excluded.name,
                model = excluded.model,
                last_seen = COALESCE(excluded.last_seen, monitors.last_seen)
        """, monitors)


def live_worker():
    while True:
        try:
            poll_current()
            STATUS["last_poll"] = datetime.now(timezone.utc)
            STATUS["last_error"] = None
        except Exception as error:     # keep collecting whatever happens
            STATUS["last_error"] = str(error)
            log.warning("Live poll failed: %s", error)
        time.sleep(REFRESH_SECONDS)


def fill_gap_since_last_run():
    """Fetch readings missed while the app was not running."""
    now = datetime.now(timezone.utc)
    started = query("SELECT location_id FROM backfill")
    for location_id in started["location_id"]:
        latest = query(
            "SELECT MAX(ts) AS ts FROM measurements WHERE location_id = ?",
            [int(location_id)],
        )["ts"].iloc[0]
        if not latest:
            continue
        cursor = parse_utc(latest)
        while cursor < now - timedelta(minutes=5):
            window_end = min(cursor + timedelta(days=BACKFILL_WINDOW_DAYS), now)
            save_rows(fetch_past(int(location_id), cursor, window_end))
            cursor = window_end
            time.sleep(0.3)


def backfill_worker():
    """Walk back through each monitor's history, 10 days per request.

    Monitors take turns, so all of them get their recent history first.
    Progress is saved, so a restart continues where it stopped.
    """
    try:
        fill_gap_since_last_run()
    except Exception as error:
        log.warning("Gap fill failed: %s", error)

    now_text = utc_str(datetime.now(timezone.utc))
    with closing(connect()) as con, con:
        con.executemany(
            "INSERT OR IGNORE INTO backfill (location_id, oldest_done) "
            "VALUES (?, ?)",
            [(m, now_text) for m in MONITOR_IDS],
        )

    while True:
        pending = query(
            f"SELECT location_id, oldest_done FROM backfill "
            f"WHERE complete = 0 AND location_id IN "
            f"({','.join('?' * len(MONITOR_IDS))})",
            MONITOR_IDS,
        )
        if pending.empty:
            log.info("History backfill complete.")
            return

        for location_id, oldest in pending.itertuples(index=False):
            end = parse_utc(oldest)
            start = max(end - timedelta(days=BACKFILL_WINDOW_DAYS),
                        HISTORY_START)
            try:
                save_rows(fetch_past(int(location_id), start, end))
            except Exception as error:
                log.warning("Backfill %s failed: %s", location_id, error)
                time.sleep(60)
                continue
            with closing(connect()) as con, con:
                con.execute(
                    "UPDATE backfill SET oldest_done = ?, complete = ? "
                    "WHERE location_id = ?",
                    (utc_str(start), int(start <= HISTORY_START),
                     int(location_id)),
                )
            time.sleep(0.3)


_workers_started = False


def start_workers():
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    threading.Thread(target=live_worker, daemon=True,
                     name="live").start()
    threading.Thread(target=backfill_worker, daemon=True,
                     name="backfill").start()


# ============================================================
# 7. READING DATA FOR THE SITE
# ============================================================

def monitor_names():
    names = {m: f"Monitor {m}" for m in MONITOR_IDS}
    df = query("SELECT location_id, name FROM monitors")
    for location_id, name in df.itertuples(index=False):
        if int(location_id) in names and name:
            names[int(location_id)] = name
    return names


def coverage():
    """First and last reading (local time) of each monitor."""
    df = query(f"""
        SELECT location_id,
               datetime(MIN(ts), '{SQL_OFFSET}') AS first,
               datetime(MAX(ts), '{SQL_OFFSET}') AS last,
               COUNT(*) AS readings
        FROM measurements GROUP BY location_id
    """)
    return df[df["location_id"].isin(MONITOR_IDS)]


def backfill_progress():
    df = query("SELECT oldest_done, complete FROM backfill")
    if df.empty:
        return 0.0
    now = datetime.now(timezone.utc)
    total = (now - HISTORY_START).total_seconds()
    done = [
        1.0 if c else (now - parse_utc(o)).total_seconds() / total
        for o, c in df.itertuples(index=False)
    ]
    return min(1.0, sum(done) / len(MONITOR_IDS))


BUCKETS = {
    "hour": "%Y-%m-%d %H:00:00",
    "day": "%Y-%m-%d",
    "month": "%Y-%m-01",
}


def load_series(monitors, start, end, resolution, cols=COLS):
    """Readings in [start, end) (UTC datetimes), times in local time.

    resolution: 'raw' (5-minute readings) or a key of BUCKETS (averages).
    """
    if not monitors:
        return pd.DataFrame(columns=["location_id", "t", *cols])
    marks = ",".join("?" * len(monitors))
    where = (f"location_id IN ({marks}) AND ts >= ? AND ts < ?")
    params = [*monitors, utc_str(start), utc_str(end)]

    if resolution == "raw":
        sql = (f"SELECT location_id, datetime(ts, '{SQL_OFFSET}') AS t, "
               f"{', '.join(cols)} FROM measurements WHERE {where} "
               f"ORDER BY t")
    else:
        bucket = f"strftime('{BUCKETS[resolution]}', ts, '{SQL_OFFSET}')"
        averages = ", ".join(f"AVG({c}) AS {c}" for c in cols)
        sql = (f"SELECT location_id, {bucket} AS t, {averages}, "
               f"COUNT(*) AS readings FROM measurements WHERE {where} "
               f"GROUP BY location_id, t ORDER BY t")

    df = query(sql, params)
    df["t"] = pd.to_datetime(df["t"])
    return df


def load_hour_of_day(monitors, start, end, col):
    marks = ",".join("?" * len(monitors))
    df = query(f"""
        SELECT location_id,
               CAST(strftime('%H', ts, '{SQL_OFFSET}') AS INTEGER) AS hour,
               AVG({col}) AS value
        FROM measurements
        WHERE location_id IN ({marks}) AND ts >= ? AND ts < ?
        GROUP BY location_id, hour ORDER BY hour
    """, [*monitors, utc_str(start), utc_str(end)])
    return df


def load_summary(monitors, start, end):
    marks = ",".join("?" * len(monitors))
    stats = ", ".join(
        f"AVG({c}) AS {c}_avg, MIN({c}) AS {c}_min, MAX({c}) AS {c}_max, "
        f"COUNT({c}) AS {c}_n"
        for c in COLS
    )
    return query(
        f"SELECT location_id, {stats} FROM measurements "
        f"WHERE location_id IN ({marks}) AND ts >= ? AND ts < ? "
        f"GROUP BY location_id",
        [*monitors, utc_str(start), utc_str(end)],
    )


def load_latest(monitors):
    marks = ",".join("?" * len(monitors))
    return query(f"""
        SELECT m.location_id, m.ts, {', '.join('m.' + c for c in COLS)}
        FROM measurements m
        JOIN (SELECT location_id, MAX(ts) AS ts FROM measurements
              WHERE location_id IN ({marks}) GROUP BY location_id) x
          ON m.location_id = x.location_id AND m.ts = x.ts
    """, monitors)


# ============================================================
# 8. TIME RANGE AND RESOLUTION
# ============================================================

QUICK_RANGES = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "1y": timedelta(days=365),
}

RES_LABEL = {"raw": "5-minute readings", "hour": "hourly averages",
             "day": "daily averages", "month": "monthly averages"}

# The finest resolution allowed for a span, to keep pages fast.
RAW_MAX = timedelta(days=14)
HOURLY_MAX = timedelta(days=180)


def local_now():
    return datetime.now(timezone.utc) + OFFSET


def selected_range(quick, start_date, end_date):
    """(start, end) in UTC for the chosen range."""
    now = datetime.now(timezone.utc)
    if quick in QUICK_RANGES:
        return now - QUICK_RANGES[quick], now + timedelta(minutes=1)
    if quick == "all":
        cov = coverage()
        if cov.empty:
            return now - timedelta(days=1), now
        first = pd.to_datetime(cov["first"]).min().to_pydatetime()
        return (first - OFFSET).replace(tzinfo=timezone.utc), \
            now + timedelta(minutes=1)
    # Custom: whole local days from start_date to end_date.
    start = datetime.fromisoformat(start_date[:10])
    end = datetime.fromisoformat(end_date[:10]) + timedelta(days=1)
    return ((start - OFFSET).replace(tzinfo=timezone.utc),
            (end - OFFSET).replace(tzinfo=timezone.utc))


def choose_resolution(choice, span):
    """The resolution to draw, and a note if the choice was coarsened."""
    if choice == "auto":
        if span <= timedelta(days=2):
            return "raw", None
        if span <= timedelta(days=60):
            return "hour", None
        return "day", None
    if choice == "raw" and span > RAW_MAX:
        return ("hour" if span <= HOURLY_MAX else "day",
                "5-minute readings are available for ranges up to 14 days; "
                "showing averages instead.")
    if choice == "hour" and span > HOURLY_MAX:
        return "day", ("Hourly averages are available for ranges up to "
                       "180 days; showing daily averages instead.")
    return choice, None


def gap_threshold(resolution):
    return {"raw": pd.Timedelta(minutes=20), "hour": pd.Timedelta(hours=3),
            "day": pd.Timedelta(days=2),
            "month": pd.Timedelta(days=62)}[resolution]


def with_gaps(part, threshold):
    """Insert blank rows where data is missing so lines break there."""
    jumps = part["t"].diff() > threshold
    if not jumps.any():
        return part
    blanks = pd.DataFrame({
        "t": part.loc[jumps, "t"] - pd.Timedelta(seconds=1)
    })
    return pd.concat([part, blanks]).sort_values("t")


# ============================================================
# 9. CHARTS
# ============================================================

def style(fig, height=360, ytitle=None, legend=True, hover="x unified"):
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=56, r=16, t=36 if legend else 12, b=40),
        font=dict(family=FONT, size=12, color=TEXT_2),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        hovermode=hover,
        hoverlabel=dict(bgcolor="white", bordercolor=GRID,
                        font=dict(family=FONT, color=TEXT_1, size=12)),
        showlegend=legend,
        legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom",
                    font=dict(color=TEXT_1), title=None),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, tickcolor=GRID,
                     ticks="outside", zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID,
                     title=dict(text=ytitle, font=dict(color=TEXT_2)))
    return fig


def empty_figure(message="No data for this selection", height=320):
    fig = go.Figure()
    fig.add_annotation(text=message, x=0.5, y=0.5, xref="paper",
                       yref="paper", showarrow=False,
                       font=dict(size=13, color=MUTED))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return style(fig, height=height, legend=False)


def fig_over_time(df, col, monitors, names, resolution, height=380,
                  legend=True, slider=True, uirev=None):
    if df.empty or df[col].isna().all():
        return empty_figure(height=height)
    info = METRICS[col]
    fig = go.Figure()
    threshold = gap_threshold(resolution)
    for m in monitors:
        part = df[df["location_id"] == m][["t", col]].dropna()
        if part.empty:
            continue
        part = with_gaps(part, threshold)
        sparse = len(part) <= 60
        fig.add_trace(go.Scatter(
            x=part["t"], y=part[col].round(info["dp"]),
            name=names[m], mode="lines+markers" if sparse else "lines",
            line=dict(color=COLOR[m], width=2),
            marker=dict(size=8, symbol=SYMBOL[m], color=COLOR[m],
                        line=dict(width=2, color=SURFACE)),
            connectgaps=False,
            hovertemplate=f"%{{y}} {info['unit']}<extra>{names[m]}</extra>",
        ))
    style(fig, height=height, ytitle=info["unit"], legend=legend)
    if slider:
        fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.07,
                                          bgcolor="#f6f6f4"))
    fig.update_layout(uirevision=uirev)
    return fig


def fig_average_bars(summary, col, monitors, names):
    info = METRICS[col]
    rows = summary.set_index("location_id")
    present = [m for m in monitors
               if m in rows.index and pd.notna(rows.loc[m, f"{col}_avg"])]
    if not present:
        return empty_figure()
    fig = go.Figure()
    for m in present:
        avg = rows.loc[m, f"{col}_avg"]
        fig.add_trace(go.Bar(
            y=[names[m]], x=[avg], orientation="h", name=names[m],
            marker=dict(color=COLOR[m], cornerradius=4),
            text=[f"{avg:.{info['dp']}f}"], textposition="outside",
            textfont=dict(color=TEXT_1),
            customdata=[[rows.loc[m, f"{col}_min"], rows.loc[m, f"{col}_max"],
                         rows.loc[m, f"{col}_n"]]],
            hovertemplate=(
                f"<b>{names[m]}</b><br>Average %{{x:.{info['dp']}f}} "
                f"{info['unit']}<br>Lowest %{{customdata[0]:.{info['dp']}f}}"
                f" · Highest %{{customdata[1]:.{info['dp']}f}}"
                f"<br>%{{customdata[2]:,}} readings<extra></extra>"
            ),
        ))
    style(fig, height=80 + 56 * len(present), ytitle=None, legend=False,
          hover="closest")
    fig.update_layout(bargap=0.35, margin=dict(l=16, r=48, t=12, b=40))
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)",
                     automargin=True, ticks="")
    fig.update_xaxes(showgrid=True, gridcolor=GRID,
                     title=dict(text=info["unit"]))
    return fig


def fig_period_bars(df, col, monitors, names, period):
    if df.empty or df[col].isna().all():
        return empty_figure()
    info = METRICS[col]
    fmt = {"hour": "%d %b %H:00", "day": "%d %b %Y",
           "month": "%b %Y"}[period]
    fig = go.Figure()
    for m in monitors:
        part = df[df["location_id"] == m].dropna(subset=[col])
        if part.empty:
            continue
        fig.add_trace(go.Bar(
            x=part["t"], y=part[col].round(info["dp"]), name=names[m],
            marker=dict(color=COLOR[m], cornerradius=4,
                        line=dict(width=0)),
            hovertemplate=(f"%{{y}} {info['unit']}"
                           f"<extra>{names[m]}</extra>"),
        ))
    style(fig, height=380, ytitle=info["unit"])
    fig.update_layout(barmode="group", bargap=0.25, bargroupgap=0.08)
    fig.update_xaxes(tickformat=fmt, hoverformat=fmt)
    if info.get("who") and period == "day":
        fig.add_hline(
            y=info["who"], line=dict(color=TEXT_2, width=1.5, dash="dot"),
            annotation_text=f"WHO 24-hour guideline ({info['who']} "
                            f"{info['unit']})",
            annotation_position="top left",
            annotation_font=dict(color=TEXT_2, size=11),
        )
    return fig


def fig_hour_of_day(df, col, monitors, names):
    if df.empty or df["value"].isna().all():
        return empty_figure()
    info = METRICS[col]
    fig = go.Figure()
    for m in monitors:
        part = df[df["location_id"] == m].dropna()
        if part.empty:
            continue
        fig.add_trace(go.Scatter(
            x=part["hour"], y=part["value"].round(info["dp"]),
            name=names[m], mode="lines+markers",
            line=dict(color=COLOR[m], width=2),
            marker=dict(size=8, symbol=SYMBOL[m], color=COLOR[m],
                        line=dict(width=2, color=SURFACE)),
            hovertemplate=f"%{{y}} {info['unit']}<extra>{names[m]}</extra>",
        ))
    style(fig, height=340, ytitle=info["unit"])
    fig.update_xaxes(
        tickmode="array", tickvals=list(range(0, 24, 3)),
        ticktext=[f"{h:02d}:00" for h in range(0, 24, 3)],
        title=dict(text=f"Hour of day ({TZ_LABEL})"),
        range=[-0.5, 23.5],
    )
    return fig


def fig_correlation(df, col, monitors, names):
    if len(monitors) < 2 or df.empty:
        return empty_figure("Select two or more monitors to compare")
    wide = df.pivot_table(index="t", columns="location_id", values=col)
    wide = wide[[m for m in monitors
                 if m in wide.columns and wide[m].count() >= 24]]
    if wide.shape[1] < 2:
        return empty_figure("Not enough overlapping data to compare yet")
    # Pairwise: each pair uses the hours both monitors have data for.
    corr = wide.corr(min_periods=24)
    labels = [names[m] for m in corr.columns]
    z = corr.to_numpy()
    fig = go.Figure(go.Heatmap(
        z=z, x=labels, y=labels, zmin=-1, zmax=1, colorscale=DIVERGING,
        xgap=2, ygap=2,
        text=[["–" if np.isnan(v) else f"{v:.2f}" for v in row]
              for row in z],
        texttemplate="%{text}", textfont=dict(color=TEXT_1, size=13),
        colorbar=dict(title=dict(text="r"), thickness=12, outlinewidth=0),
        hovertemplate="%{y} vs %{x}<br>r = %{z:.2f}<extra></extra>",
    ))
    style(fig, height=360, legend=False, hover="closest")
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(side="bottom")
    return fig


# ============================================================
# 10. PAGE PIECES
# ============================================================

def aqi_band(pm25):
    for limit, label, color in AQI_BANDS:
        if pm25 <= limit:
            return label, color
    return AQI_BANDS[-1][1:]


def fmt(value, dp):
    return "–" if value is None or pd.isna(value) else f"{value:,.{dp}f}"


def monitor_card(m, names, latest):
    rows = latest[latest["location_id"] == m]
    head = html.Div([
        html.Span(className="swatch", style={"background": COLOR[m]}),
        html.Span(names[m], className="card-title"),
    ], className="card-head")

    if rows.empty:
        return html.Div([head, html.P("No readings yet.", className="muted")],
                        className="card monitor-card")

    r = rows.iloc[0]
    seen = parse_utc(r["ts"])
    age = datetime.now(timezone.utc) - seen
    minutes = int(age.total_seconds() // 60)
    if minutes < 1:
        age_text = "just now"
    elif minutes < 90:
        age_text = f"{minutes} min ago"
    elif minutes < 48 * 60:
        age_text = f"{minutes // 60} h ago"
    else:
        age_text = f"{minutes // 1440} days ago"
    offline = age > timedelta(minutes=STALE_MINUTES)

    pm = r["pm25"]
    if pd.notna(pm):
        band, band_color = aqi_band(pm)
        badge = html.Span([
            html.Span(className="dot", style={"background": band_color}),
            band,
        ], className="aqi")
    else:
        badge = None

    small = [
        ("PM10", fmt(r["pm10"], 1), "µg/m³"),
        ("Temp", fmt(r["temp"], 1), "°C"),
        ("Humidity", fmt(r["rh"], 0), "%"),
        ("CO₂", fmt(r["co2"], 0), "ppm"),
        ("TVOC", fmt(r["tvoc"], 0), ""),
        ("NOx", fmt(r["nox"], 0), ""),
    ]

    return html.Div([
        head,
        html.Div([
            html.Span("Offline" if offline else "Live",
                      className="pill offline" if offline else "pill live"),
            html.Span(
                f"{(seen + OFFSET).strftime('%d %b, %H:%M')} {TZ_LABEL} "
                f"· {age_text}", className="muted small"),
        ], className="card-sub"),
        html.Div([
            html.Span(fmt(pm, 1), className="hero"),
            html.Span(" µg/m³ PM2.5", className="hero-unit"),
        ]),
        badge,
        html.Div([
            html.Div([html.Div(label, className="mini-label"),
                      html.Div([value, html.Span(f" {unit}",
                                                 className="mini-unit")],
                               className="mini-value")],
                     className="mini")
            for label, value, unit in small
        ], className="mini-grid"),
    ], className="card monitor-card")


def card(title, body, subtitle=None, extra=None):
    return html.Div([
        html.Div([
            html.Div([html.H3(title),
                      html.P(subtitle, className="muted small")
                      if subtitle else None]),
            extra,
        ], className="card-header"),
        body,
    ], className="card")


def table(df, page_size=15, id=None):
    if df.empty:
        return html.P("No data for this selection.", className="muted")
    props = dict(
        data=df.to_dict("records"),
        columns=[{"name": c, "id": c} for c in df.columns],
        page_size=page_size,
        sort_action="native",
        filter_action="native",
        style_as_list_view=True,
        style_table={"overflowX": "auto"},
        style_cell={"padding": "8px 12px", "fontFamily": FONT,
                    "fontSize": 13, "textAlign": "right",
                    "color": TEXT_1, "border": "none",
                    "borderBottom": f"1px solid {GRID}"},
        style_cell_conditional=[
            {"if": {"column_id": c}, "textAlign": "left"}
            for c in ("Monitor", "Parameter", f"Time ({TZ_LABEL})")
        ],
        style_header={"fontWeight": 600, "background": "#f6f6f4",
                      "color": TEXT_2, "borderBottom": f"1px solid {GRID}"},
        style_filter={"background": "#fbfbfa"},
    )
    if id:
        props["id"] = id
    return dash_table.DataTable(**props)


def monitor_options(names):
    return [{
        "label": html.Span([
            html.Span(className="swatch", style={"background": COLOR[m]}),
            names[m],
        ], className="check-label"),
        "value": m,
    } for m in MONITOR_IDS]


def legend_row(monitors, names):
    return html.Div([
        html.Span([html.Span(className="swatch",
                             style={"background": COLOR[m]}), names[m]],
                  className="legend-item")
        for m in monitors
    ], className="legend-row")


# ============================================================
# 11. LAYOUT
# ============================================================

init_database()

app = Dash(__name__, title="Karachi Outdoor Air Quality")
server = app.server


def serve_layout():
    names = monitor_names()
    today = local_now().date()
    return html.Div([
        html.Header([
            html.Div([
                html.H1("Outdoor Air Quality"),
                html.P(f"{len(MONITOR_IDS)} AirGradient outdoor monitors · "
                       f"times in {TZ_LABEL} (UTC"
                       f"{UTC_OFFSET_HOURS:+.0f})", className="muted"),
            ]),
            html.Div(id="status", className="status"),
        ], className="page-header"),

        dcc.Interval(id="tick", interval=REFRESH_SECONDS * 1000),

        html.Div([
            html.Div([
                html.Label("Monitors"),
                dcc.Checklist(
                    id="monitors",
                    options=monitor_options(names),
                    value=list(MONITOR_IDS),
                    inline=True,
                    className="checklist",
                ),
            ], className="control wide"),

            html.Div([
                html.Label("Time range"),
                dcc.RadioItems(
                    id="quick",
                    options=[{"label": l, "value": v} for l, v in [
                        ("24 h", "24h"), ("7 days", "7d"),
                        ("30 days", "30d"), ("90 days", "90d"),
                        ("1 year", "1y"), ("All", "all"),
                        ("Custom", "custom")]],
                    value="7d", inline=True, className="segmented",
                ),
                dcc.DatePickerRange(
                    id="dates",
                    display_format="DD MMM YYYY",
                    start_date=(today - timedelta(days=7)).isoformat(),
                    end_date=today.isoformat(),
                    max_date_allowed=today.isoformat(),
                    minimum_nights=0,
                    first_day_of_week=1,
                ),
                html.Div(id="coverage", className="muted small"),
            ], className="control wide"),

            html.Div([
                html.Label("Parameter"),
                dcc.Dropdown(
                    id="param",
                    options=[{"label": f"{v['label']} ({v['unit']})",
                              "value": k} for k, v in METRICS.items()],
                    value="pm25", clearable=False,
                ),
            ], className="control"),

            html.Div([
                html.Label("Detail"),
                dcc.RadioItems(
                    id="resolution",
                    options=[{"label": l, "value": v} for l, v in [
                        ("Auto", "auto"), ("5 min", "raw"),
                        ("Hourly", "hour"), ("Daily", "day")]],
                    value="auto", inline=True, className="segmented",
                ),
            ], className="control"),
        ], className="controls card"),

        html.Div(id="note", className="note"),

        html.H2("Latest readings"),
        html.Div(id="cards", className="cards"),

        html.H2("Compare monitors"),
        dcc.Loading(html.Div(id="charts"), type="dot", color=PALETTE[0]),

        html.H2("All parameters"),
        dcc.Loading(html.Div(id="small-multiples"), type="dot",
                    color=PALETTE[0]),

        html.H2("Summary"),
        dcc.Loading(html.Div(id="summary"), type="dot", color=PALETTE[0]),

        html.H2("Data"),
        html.Div([
            html.Button("Download CSV", id="dl-csv", className="btn"),
            html.Button("Download Excel", id="dl-xlsx", className="btn"),
            dcc.Download(id="download"),
        ], className="actions"),
        dcc.Loading(html.Div(id="data-table"), type="dot",
                    color=PALETTE[0]),

        html.Footer(
            "Data: AirGradient. PM values are AirGradient's corrected "
            "readings. Air-quality categories use US EPA PM2.5 "
            "breakpoints.", className="muted small footer"),
    ], className="page")


app.layout = serve_layout


# ============================================================
# 12. CALLBACKS
# ============================================================

@app.callback(
    Output("quick", "value"),
    Output("dates", "start_date"),
    Output("dates", "end_date"),
    Input("quick", "value"),
    Input("dates", "start_date"),
    Input("dates", "end_date"),
)
def sync_range(quick, start_date, end_date):
    """Quick ranges fill in the date picker; editing the dates = Custom."""
    if ctx.triggered_id == "dates":
        return "custom", no_update, no_update
    if quick == "custom":
        return no_update, no_update, no_update
    start, end = selected_range(quick, start_date, end_date)
    end = min(end, datetime.now(timezone.utc))
    return (no_update, (start + OFFSET).date().isoformat(),
            (end + OFFSET).date().isoformat())


@app.callback(
    Output("status", "children"),
    Output("coverage", "children"),
    Output("dates", "min_date_allowed"),
    Output("dates", "max_date_allowed"),
    Output("monitors", "options"),
    Input("tick", "n_intervals"),
)
def update_status(_):
    names = monitor_names()
    options = monitor_options(names)
    cov = coverage()
    progress = backfill_progress()
    parts = []
    if STATUS["last_error"]:
        parts.append(html.Span("Live updates paused: cannot reach "
                               "AirGradient", className="pill offline"))
    elif STATUS["last_poll"]:
        stamp = (STATUS["last_poll"] + OFFSET).strftime("%H:%M")
        parts.append(html.Span(f"Live · updated {stamp} {TZ_LABEL}",
                               className="pill live"))
    if progress < 1:
        parts.append(html.Span(f"Loading history… {progress:.0%}",
                               className="pill loading"))

    today = local_now().date().isoformat()
    if cov.empty:
        return parts, "No data stored yet.", None, today, options
    first = pd.to_datetime(cov["first"]).min()
    text = (f"Data available from {first:%d %b %Y}"
            + ("" if progress >= 1 else " (still loading older data)"))
    return parts, text, first.date().isoformat(), today, options


@app.callback(
    Output("note", "children"),
    Output("cards", "children"),
    Output("charts", "children"),
    Output("small-multiples", "children"),
    Output("summary", "children"),
    Output("data-table", "children"),
    Input("monitors", "value"),
    Input("quick", "value"),
    Input("dates", "start_date"),
    Input("dates", "end_date"),
    Input("param", "value"),
    Input("resolution", "value"),
    Input("tick", "n_intervals"),
)
def update_page(monitors, quick, start_date, end_date, col, res_choice, _):
    names = monitor_names()
    monitors = [m for m in MONITOR_IDS if m in (monitors or [])]

    if not monitors:
        msg = html.P("Select at least one monitor.", className="muted")
        return None, msg, msg, None, None, None

    # A tick only matters when the range runs up to now.
    if ctx.triggered_id == "tick" and quick == "custom" and \
            end_date and end_date[:10] < local_now().date().isoformat():
        return (no_update,) * 6

    start, end = selected_range(quick, start_date, end_date)
    span = end - start
    resolution, note = choose_resolution(res_choice, span)
    info = METRICS[col]
    uirev = f"{col}-{quick}-{start_date}-{end_date}-{resolution}"

    latest = load_latest(monitors)
    cards = [monitor_card(m, names, latest) for m in monitors]

    series = load_series(monitors, start, end, resolution)
    hourly = series if resolution == "hour" else \
        load_series(monitors, start, end, "hour", cols=[col])

    period = "hour" if span <= timedelta(days=2) else \
        "day" if span <= timedelta(days=62) else "month"
    period_df = hourly if period == "hour" else \
        load_series(monitors, start, end, period, cols=[col])

    summary = load_summary(monitors, start, end)
    hour_of_day = load_hour_of_day(monitors, start, end, col)

    label = info["label"]
    charts = html.Div([
        card(f"{label} over time",
             dcc.Graph(figure=fig_over_time(series, col, monitors, names,
                                            resolution, uirev=uirev),
                       config={"displaylogo": False}),
             subtitle=f"{RES_LABEL[resolution].capitalize()} · drag on the "
                      f"chart or the slider below it to zoom in"),
        html.Div([
            card(f"Average {label}",
                 dcc.Graph(figure=fig_average_bars(summary, col, monitors,
                                                   names),
                           config={"displayModeBar": False}),
                 subtitle="Whole selected range · hover for lowest, "
                          "highest and reading count"),
            card(f"{label} by hour of day",
                 dcc.Graph(figure=fig_hour_of_day(hour_of_day, col,
                                                  monitors, names),
                           config={"displayModeBar": False}),
                 subtitle="Average for each hour across the range"),
        ], className="grid-2"),
        card(f"{RES_LABEL[period].capitalize().replace('averages', 'average')}"
             f" {label}",
             dcc.Graph(figure=fig_period_bars(period_df, col, monitors,
                                              names, period),
                       config={"displaylogo": False}),
             subtitle=f"Monitors side by side for each {period}"),
        card(f"How closely the monitors agree on {label}",
             dcc.Graph(figure=fig_correlation(hourly, col, monitors, names),
                       config={"displayModeBar": False}),
             subtitle="Correlation of hourly averages: 1 = rise and fall "
                      "together, 0 = unrelated"),
    ])

    small = html.Div([
        legend_row(monitors, names),
        html.Div([
            card(f"{v['label']} ({v['unit']})",
                 dcc.Graph(
                     figure=fig_over_time(series, c, monitors, names,
                                          resolution, height=240,
                                          legend=False, slider=False,
                                          uirev=uirev),
                     config={"displayModeBar": False}))
            for c, v in METRICS.items()
        ], className="grid-small"),
    ])

    stats = []
    for m in monitors:
        row = summary[summary["location_id"] == m]
        if row.empty:
            continue
        row = row.iloc[0]
        for c, v in METRICS.items():
            if not row[f"{c}_n"]:
                continue
            stats.append({
                "Monitor": names[m], "Parameter": v["label"],
                "Unit": v["unit"],
                "Average": round(row[f"{c}_avg"], v["dp"]),
                "Lowest": round(row[f"{c}_min"], v["dp"]),
                "Highest": round(row[f"{c}_max"], v["dp"]),
                "Readings": int(row[f"{c}_n"]),
            })
    summary_table = card(
        "Statistics for the selected range",
        table(pd.DataFrame(stats), page_size=14),
        subtitle="Sort or filter any column",
    )

    data = to_export_frame(series, names, resolution)
    shown = data.iloc[::-1].head(2000)
    data_card = card(
        "Readings",
        table(shown, page_size=20),
        subtitle=(f"{RES_LABEL[resolution].capitalize()}, newest first"
                  + (f" · showing the latest 2,000 of {len(data):,} rows; "
                     "download for all of them" if len(data) > 2000 else "")),
    )

    note_box = html.Div(note, className="note-box") if note else None
    return note_box, cards, charts, small, summary_table, data_card


def to_export_frame(series, names, resolution):
    if series.empty:
        return pd.DataFrame()
    out = series.copy()
    out.insert(0, "Monitor", out["location_id"].map(names))
    time_fmt = {"raw": "%Y-%m-%d %H:%M", "hour": "%Y-%m-%d %H:00",
                "day": "%Y-%m-%d", "month": "%Y-%m"}[resolution]
    out[f"Time ({TZ_LABEL})"] = out["t"].dt.strftime(time_fmt)
    out = out.drop(columns=["location_id", "t"])
    rename = {c: f"{v['label']} ({v['unit']})" for c, v in METRICS.items()}
    for c, v in METRICS.items():
        out[c] = out[c].round(v["dp"])
    if "readings" in out:
        out = out.rename(columns={"readings": "Readings averaged"})
    cols = ["Monitor", f"Time ({TZ_LABEL})", *COLS] + \
        (["Readings averaged"] if "Readings averaged" in out else [])
    return out[cols].rename(columns=rename)


@app.callback(
    Output("download", "data"),
    Input("dl-csv", "n_clicks"),
    Input("dl-xlsx", "n_clicks"),
    State("monitors", "value"),
    State("quick", "value"),
    State("dates", "start_date"),
    State("dates", "end_date"),
    State("resolution", "value"),
    prevent_initial_call=True,
)
def download(_csv, _xlsx, monitors, quick, start_date, end_date, res_choice):
    names = monitor_names()
    monitors = [m for m in MONITOR_IDS if m in (monitors or [])]
    if not monitors:
        return no_update
    start, end = selected_range(quick, start_date, end_date)
    resolution, _ = choose_resolution(res_choice, end - start)
    data = to_export_frame(load_series(monitors, start, end, resolution),
                           names, resolution)
    if data.empty:
        return no_update
    stem = (f"air_quality_{(start + OFFSET):%Y%m%d}-"
            f"{(min(end, datetime.now(timezone.utc)) + OFFSET):%Y%m%d}")

    if ctx.triggered_id == "dl-csv":
        # utf-8-sig so Excel shows µ, ² and ₂ correctly.
        return dcc.send_bytes(
            lambda buffer: buffer.write(
                data.to_csv(index=False).encode("utf-8-sig")),
            f"{stem}.csv",
        )

    summary = load_summary(monitors, start, end)
    stats = []
    for row in summary.itertuples(index=False):
        row = row._asdict()
        for c, v in METRICS.items():
            stats.append({
                "Monitor": names[row["location_id"]],
                "Parameter": v["label"], "Unit": v["unit"],
                "Average": row[f"{c}_avg"], "Lowest": row[f"{c}_min"],
                "Highest": row[f"{c}_max"], "Readings": row[f"{c}_n"],
            })

    def write(buffer):
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            data.to_excel(writer, sheet_name="Readings", index=False)
            pd.DataFrame(stats).to_excel(writer, sheet_name="Summary",
                                         index=False)

    return dcc.send_bytes(write, f"{stem}.xlsx")


# ============================================================
# 13. START
# ============================================================

start_workers()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8050")),
            debug=False)
