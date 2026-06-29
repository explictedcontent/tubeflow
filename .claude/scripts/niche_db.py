#!/usr/bin/env python3
"""
SQLite store for the TubeFlow niche finder.

Accumulates channels, videos, and periodic stat snapshots across runs so the
finder can do two things a one-shot snapshot tool cannot:

  1. Score channel TRAJECTORY (sub growth, view acceleration, outlier
     consistency) - the signal that actually predicts a repeatable breakout.
  2. BACKTEST its own virality score against real 30-day outcomes.

The DB lives at .claude/scripts/.niche_db.sqlite (gitignored). It is additive:
every run upserts entities and appends a snapshot, so history grows over time.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).parent / ".niche_db.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id   TEXT PRIMARY KEY,
    title        TEXT,
    country      TEXT,
    created_at   TEXT,
    first_seen   TEXT,
    last_polled  TEXT
);
CREATE TABLE IF NOT EXISTS channel_snapshots (
    channel_id   TEXT,
    polled_at    TEXT,
    subscribers  INTEGER,
    video_count  INTEGER,
    total_views  INTEGER,
    PRIMARY KEY (channel_id, polled_at)
);
CREATE TABLE IF NOT EXISTS videos (
    video_id     TEXT PRIMARY KEY,
    channel_id   TEXT,
    title        TEXT,
    niche        TEXT,
    published_at TEXT,
    thumbnail    TEXT,
    first_seen   TEXT,
    last_polled  TEXT
);
CREATE TABLE IF NOT EXISTS video_snapshots (
    video_id     TEXT,
    polled_at    TEXT,
    views        INTEGER,
    likes        INTEGER,
    comments     INTEGER,
    PRIMARY KEY (video_id, polled_at)
);
CREATE TABLE IF NOT EXISTS predictions (
    video_id              TEXT,
    predicted_at          TEXT,
    niche                 TEXT,
    virality_score        REAL,
    subs_at_prediction    INTEGER,
    views_at_prediction   INTEGER,
    outlier_at_prediction REAL,
    PRIMARY KEY (video_id, predicted_at)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the DB (creating schema if needed) with dict-like rows."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# === WRITES ===

def record_channel(conn, channel_id, title, country, created_at,
                   subs, video_count, total_views, ts=None) -> None:
    """Upsert a channel and append a stat snapshot."""
    if not channel_id:
        return
    ts = ts or now_iso()
    conn.execute(
        """INSERT INTO channels (channel_id, title, country, created_at, first_seen, last_polled)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(channel_id) DO UPDATE SET
             title=excluded.title, country=excluded.country, last_polled=excluded.last_polled""",
        (channel_id, title, country, created_at, ts, ts),
    )
    conn.execute(
        """INSERT OR IGNORE INTO channel_snapshots
           (channel_id, polled_at, subscribers, video_count, total_views)
           VALUES (?, ?, ?, ?, ?)""",
        (channel_id, ts, subs, video_count, total_views),
    )


def record_video(conn, video_id, channel_id, title, niche, published_at,
                 thumbnail, views, likes, comments, ts=None) -> None:
    """Upsert a video and append a stat snapshot."""
    if not video_id:
        return
    ts = ts or now_iso()
    conn.execute(
        """INSERT INTO videos
           (video_id, channel_id, title, niche, published_at, thumbnail, first_seen, last_polled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(video_id) DO UPDATE SET
             title=excluded.title, niche=excluded.niche,
             thumbnail=excluded.thumbnail, last_polled=excluded.last_polled""",
        (video_id, channel_id, title, niche, published_at, thumbnail, ts, ts),
    )
    conn.execute(
        """INSERT OR IGNORE INTO video_snapshots (video_id, polled_at, views, likes, comments)
           VALUES (?, ?, ?, ?, ?)""",
        (video_id, ts, views, likes, comments),
    )


def log_prediction(conn, video_id, niche, score, subs, views, outlier, ts=None) -> None:
    """Record a virality prediction so it can be checked against reality later."""
    if not video_id:
        return
    ts = ts or now_iso()
    conn.execute(
        """INSERT OR IGNORE INTO predictions
           (video_id, predicted_at, niche, virality_score,
            subs_at_prediction, views_at_prediction, outlier_at_prediction)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (video_id, ts, niche, score, subs, views, outlier),
    )


# === READS ===

def channel_trajectory(conn, channel_id: str) -> dict:
    """
    Compute growth signals from a channel's snapshot history.

    Returns sub_growth_per_week, total_view_growth, span_days, n_snapshots.
    Returns zeros (and has_history=False) when there is only one snapshot.
    """
    rows = conn.execute(
        """SELECT polled_at, subscribers, total_views FROM channel_snapshots
           WHERE channel_id=? ORDER BY polled_at""",
        (channel_id,),
    ).fetchall()
    if len(rows) < 2:
        return {"has_history": False, "sub_growth_per_week": 0.0,
                "total_view_growth": 0, "span_days": 0.0, "n_snapshots": len(rows)}

    first, last = rows[0], rows[-1]
    try:
        t0 = datetime.fromisoformat(first["polled_at"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(last["polled_at"].replace("Z", "+00:00"))
        span_days = max((t1 - t0).total_seconds() / 86400.0, 0.01)
    except (TypeError, ValueError):
        span_days = 0.01

    sub_delta = (last["subscribers"] or 0) - (first["subscribers"] or 0)
    view_delta = (last["total_views"] or 0) - (first["total_views"] or 0)
    return {
        "has_history": True,
        "sub_growth_per_week": round(sub_delta / span_days * 7.0, 1),
        "total_view_growth": view_delta,
        "span_days": round(span_days, 1),
        "n_snapshots": len(rows),
    }


def known_channel_ids(conn) -> list:
    return [r["channel_id"] for r in conn.execute("SELECT channel_id FROM channels")]


def known_video_ids(conn, since_days: Optional[int] = None) -> list:
    """Video IDs known to the DB (optionally only those first seen recently)."""
    if since_days is None:
        rows = conn.execute("SELECT video_id FROM videos")
    else:
        rows = conn.execute(
            "SELECT video_id FROM videos WHERE first_seen >= ?",
            ((datetime.now(timezone.utc).timestamp() - since_days * 86400),),
        )
    return [r["video_id"] for r in rows]


def due_predictions(conn, min_age_days: int = 30) -> list:
    """Predictions old enough to evaluate against current reality."""
    rows = conn.execute(
        "SELECT * FROM predictions ORDER BY predicted_at"
    ).fetchall()
    out = []
    now = datetime.now(timezone.utc)
    for r in rows:
        try:
            t = datetime.fromisoformat(r["predicted_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if (now - t).total_seconds() / 86400.0 >= min_age_days:
            out.append(dict(r))
    return out


def stats(conn) -> dict:
    """Summary counts for status output."""
    def count(table):
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    return {
        "channels": count("channels"),
        "videos": count("videos"),
        "channel_snapshots": count("channel_snapshots"),
        "video_snapshots": count("video_snapshots"),
        "predictions": count("predictions"),
    }
