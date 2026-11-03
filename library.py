"""Channel library — remembers every channel YT Scout has checked.

Stores raw facts (not pass/fail), so a channel rejected under one set of
filters can still match a later search with different filters.

    channels  : one row per handle — facts as JSON, how far evaluation got
                ("stage"), and when it was checked
    sightings : which keyword surfaced which channel, first/last seen

The library is an accelerator, never a source of truth: any storage error
raises LibraryUnavailable and the scraper falls back to scraping.

Location: YT_SCOUT_DB env var, else yt_scout.db next to this file.
Journal mode is SQLite's default rollback journal (no -wal/-shm side files),
chosen because the project folder is synced by OneDrive.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

FRESH_DAYS = 14

# How far the pipeline got before the channel was stored:
STAGE_SUBS = "subs"          # only subscriber count known (rejected at subs gate)
STAGE_LOCATION = "location"  # + about page facts (rejected at location gate)
STAGE_COMPLETE = "complete"  # + tab metrics and category

# Facts worth keeping (about_text is large and only needed to derive th_signal)
FACT_FIELDS = (
    "name", "handle", "uc_id", "url", "channel_url", "subs", "country",
    "location_badge", "th_signal", "emails", "links", "category",
    "upload_frequency", "avg_views_videos", "avg_views_shorts", "avg_views_live",
    "metrics_days",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    handle      TEXT PRIMARY KEY,
    facts       TEXT NOT NULL,
    stage       TEXT NOT NULL,
    checked_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sightings (
    handle       TEXT NOT NULL,
    keyword      TEXT NOT NULL,
    video_title  TEXT,
    video_url    TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    PRIMARY KEY (handle, keyword)
);
"""


class LibraryUnavailable(Exception):
    pass


def db_path() -> str:
    return os.environ.get("YT_SCOUT_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "yt_scout.db")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    """Short-lived connection per call: safe across threads, nothing held open."""
    try:
        conn = sqlite3.connect(db_path(), timeout=10)
        conn.executescript(_SCHEMA)
        return conn
    except sqlite3.Error as e:
        raise LibraryUnavailable(f"cannot open {db_path()}: {e}") from e


def lookup(handle: str, now: datetime | None = None) -> tuple[dict, str, str] | None:
    """(facts, stage, checked_at) if checked within FRESH_DAYS, else None."""
    now = now or _now()
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT facts, stage, checked_at FROM channels WHERE handle = ?",
                (handle,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise LibraryUnavailable(str(e)) from e
    if not row:
        return None
    try:
        facts = json.loads(row[0])
        checked = datetime.fromisoformat(row[2])
    except (ValueError, TypeError):
        return None                      # unreadable row = miss, scrape again
    if now - checked > timedelta(days=FRESH_DAYS):
        return None
    return facts, row[1], row[2]


def save(handle: str, data: dict, stage: str, now: datetime | None = None) -> None:
    facts = {k: data.get(k) for k in FACT_FIELDS if k in data}
    stamp = (now or _now()).isoformat(timespec="seconds")
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO channels (handle, facts, stage, checked_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(handle) DO UPDATE SET facts=excluded.facts, "
                    "stage=excluded.stage, checked_at=excluded.checked_at",
                    (handle, json.dumps(facts, ensure_ascii=False), stage, stamp))
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise LibraryUnavailable(str(e)) from e


def record_sighting(handle: str, keyword: str, video_title: str, video_url: str,
                    now: datetime | None = None) -> None:
    stamp = (now or _now()).isoformat(timespec="seconds")
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO sightings (handle, keyword, video_title, video_url, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(handle, keyword) DO UPDATE SET last_seen=excluded.last_seen, "
                    "video_title=excluded.video_title, video_url=excluded.video_url",
                    (handle, keyword, video_title, video_url, stamp, stamp))
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise LibraryUnavailable(str(e)) from e


def keywords_for(handle: str) -> list[str]:
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT keyword FROM sightings WHERE handle = ? ORDER BY first_seen, rowid",
                (handle,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise LibraryUnavailable(str(e)) from e
    return [r[0] for r in rows]
