"""SQLite storage. One connection, used from the event-loop thread only."""
import json
import sqlite3
import time
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    tvmaze_id INTEGER UNIQUE NOT NULL,
    imdb_id TEXT, tvdb_id INTEGER,
    title TEXT NOT NULL, sort_title TEXT, year INTEGER,
    status TEXT, overview TEXT, network TEXT, country TEXT,
    original_language TEXT, is_anime INTEGER DEFAULT 0,
    genres TEXT DEFAULT '[]', aliases TEXT DEFAULT '[]',
    runtime INTEGER, poster TEXT, fanart TEXT,
    path TEXT NOT NULL, root_folder TEXT,
    quality_profile TEXT NOT NULL,
    language_mode TEXT NOT NULL DEFAULT 'default',
    monitored INTEGER DEFAULT 1, monitor_mode TEXT DEFAULT 'all',
    added_at REAL, refreshed_at REAL
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    tvmaze_id INTEGER,
    season INTEGER NOT NULL, number INTEGER NOT NULL, absolute INTEGER,
    title TEXT, airdate TEXT, airstamp TEXT, overview TEXT, runtime INTEGER,
    monitored INTEGER DEFAULT 1,
    file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    UNIQUE(series_id, season, number)
);
CREATE INDEX IF NOT EXISTS ix_episodes_series ON episodes(series_id);
CREATE INDEX IF NOT EXISTS ix_episodes_air ON episodes(airdate);

CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY,
    tmdb_id INTEGER UNIQUE NOT NULL,
    imdb_id TEXT,
    title TEXT NOT NULL, original_title TEXT, sort_title TEXT, year INTEGER,
    overview TEXT, status TEXT, runtime INTEGER, original_language TEXT,
    genres TEXT DEFAULT '[]', aliases TEXT DEFAULT '[]',
    poster TEXT, fanart TEXT,
    theatrical_date TEXT, digital_date TEXT, physical_date TEXT,
    path TEXT NOT NULL, root_folder TEXT,
    quality_profile TEXT NOT NULL,
    language_mode TEXT NOT NULL DEFAULT 'default',
    monitored INTEGER DEFAULT 1,
    file_id INTEGER REFERENCES media_files(id) ON DELETE SET NULL,
    added_at REAL, refreshed_at REAL
);

CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                -- episode | movie
    series_id INTEGER, movie_id INTEGER,
    path TEXT NOT NULL UNIQUE,
    size INTEGER, quality TEXT, revision INTEGER DEFAULT 1,
    release_group TEXT, release_title TEXT, languages TEXT DEFAULT '[]',
    added_at REAL,
    subtitles TEXT DEFAULT '{}',
    sub_status TEXT DEFAULT 'pending',  -- pending|ok|embedded|missing|disabled
    sub_checked_at REAL, sub_score INTEGER
);

CREATE TABLE IF NOT EXISTS indexers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                -- cardigann | torznab
    definition_id TEXT,
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 25,
    use_for TEXT DEFAULT 'both',       -- both | tv | movies
    settings TEXT DEFAULT '{}',
    flaresolverr INTEGER DEFAULT 0,
    use_proxy INTEGER DEFAULT 0,
    seed_goals INTEGER DEFAULT 1,
    query_limit INTEGER, grab_limit INTEGER,
    day TEXT, queries_today INTEGER DEFAULT 0, grabs_today INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0, disabled_until REAL, last_error TEXT, last_error_kind TEXT,
    last_ok REAL, avg_ms INTEGER, cookies TEXT DEFAULT '{}', user_agent TEXT,
    added_at REAL
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY,
    hash TEXT, title TEXT NOT NULL,
    kind TEXT NOT NULL,                -- episode | movie
    series_id INTEGER, movie_id INTEGER, episode_ids TEXT DEFAULT '[]',
    indexer_id INTEGER, indexer_name TEXT,
    quality TEXT, size INTEGER, release TEXT DEFAULT '{}',
    status TEXT NOT NULL,              -- downloading|importing|imported|failed|import_blocked|removed
    progress REAL DEFAULT 0, state TEXT, eta INTEGER, dlspeed INTEGER,
    last_progress REAL DEFAULT 0, last_progress_at REAL,
    message TEXT, added_at REAL, finished_at REAL, removed_from_client INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_downloads_status ON downloads(status);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    event TEXT NOT NULL,               -- grabbed|imported|failed|subtitle|deleted|renamed|removed|blocked
    kind TEXT, series_id INTEGER, movie_id INTEGER, episode_ids TEXT DEFAULT '[]',
    title TEXT, message TEXT, data TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_history_at ON history(at);

CREATE TABLE IF NOT EXISTS blocklist (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    title TEXT, hash TEXT, guid TEXT, indexer_name TEXT,
    series_id INTEGER, movie_id INTEGER, reason TEXT
);
"""

_conn: sqlite3.Connection | None = None


def connect(path=None) -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    config.ensure_dirs()
    _conn = sqlite3.connect(str(path or config.DB_PATH), check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(SCHEMA)
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _c() -> sqlite3.Connection:
    return _conn or connect()


def rows(sql: str, *args: Any) -> list[dict]:
    return [dict(r) for r in _c().execute(sql, args).fetchall()]


def row(sql: str, *args: Any) -> dict | None:
    r = _c().execute(sql, args).fetchone()
    return dict(r) if r else None


def scalar(sql: str, *args: Any) -> Any:
    r = _c().execute(sql, args).fetchone()
    return r[0] if r else None


def execute(sql: str, *args: Any) -> int:
    cur = _c().execute(sql, args)
    return cur.lastrowid


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    _c().executemany(sql, seq)


def insert(table: str, data: dict) -> int:
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", *data.values())


def update(table: str, id_: int, data: dict) -> None:
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", *data.values(), id_)


class transaction:
    def __enter__(self):
        _c().execute("BEGIN")
        return self

    def __exit__(self, exc_type, *_):
        _c().execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def jload(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def now() -> float:
    return time.time()


def add_history(event: str, title: str, message: str = "", kind: str | None = None,
                series_id: int | None = None, movie_id: int | None = None,
                episode_ids: list | None = None, data: dict | None = None) -> None:
    insert("history", {
        "at": now(), "event": event, "kind": kind, "series_id": series_id, "movie_id": movie_id,
        "episode_ids": jdump(episode_ids or []), "title": title, "message": message,
        "data": jdump(data or {}),
    })
