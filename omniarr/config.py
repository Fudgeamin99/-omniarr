"""Paths and environment overrides.

Everything a user would normally change lives in the database (Settings screen).
Environment variables only cover container plumbing and test hooks.
"""
import os
from pathlib import Path

VERSION = "1.0.0"
APP_NAME = "Omniarr"

CONFIG_DIR = Path(os.environ.get("OMNIARR_CONFIG", "/config"))
PORT = int(os.environ.get("OMNIARR_PORT", "8484"))
BIND = os.environ.get("OMNIARR_BIND", "0.0.0.0")

DB_PATH = CONFIG_DIR / "omniarr.db"
LOG_DIR = CONFIG_DIR / "logs"
DEFINITIONS_DIR = CONFIG_DIR / "definitions"          # downloaded / updated definitions
BUNDLED_DEFINITIONS = Path(__file__).resolve().parent.parent / "definitions"  # shipped snapshot
CUSTOM_DEFINITIONS_DIR = CONFIG_DIR / "definitions-custom"  # user-provided .yml
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

# Upstream services. Overridable so the test-suite can point at local mocks.
TVMAZE_URL = os.environ.get("OMNIARR_TVMAZE_URL", "https://api.tvmaze.com")
TMDB_URL = os.environ.get("OMNIARR_TMDB_URL", "https://api.themoviedb.org/3")
TMDB_IMAGE_URL = os.environ.get("OMNIARR_TMDB_IMAGE_URL", "https://image.tmdb.org/t/p")
OPENSUBTITLES_URL = os.environ.get("OMNIARR_OPENSUBTITLES_URL", "https://api.opensubtitles.com/api/v1")
PODNAPISI_URL = os.environ.get("OMNIARR_PODNAPISI_URL", "https://www.podnapisi.net")
DEFINITIONS_ARCHIVE_URL = os.environ.get(
    "OMNIARR_DEFINITIONS_URL",
    "https://github.com/Prowlarr/Indexers/archive/refs/heads/master.zip",
)
DEFINITIONS_VERSION = "v11"

# Scheduler intervals (seconds); shortened in tests.
INTERVALS = {
    "downloads": int(os.environ.get("OMNIARR_INTERVAL_DOWNLOADS", "30")),
    "rss": int(os.environ.get("OMNIARR_INTERVAL_RSS", str(15 * 60))),
    "refresh": int(os.environ.get("OMNIARR_INTERVAL_REFRESH", str(6 * 3600))),
    "subtitles": int(os.environ.get("OMNIARR_INTERVAL_SUBTITLES", str(6 * 3600))),
    "housekeeping": int(os.environ.get("OMNIARR_INTERVAL_HOUSEKEEPING", str(24 * 3600))),
    "definitions": int(os.environ.get("OMNIARR_INTERVAL_DEFINITIONS", str(7 * 24 * 3600))),
}
SCHEDULER_ENABLED = os.environ.get("OMNIARR_SCHEDULER", "1") == "1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
APP_USER_AGENT = f"Omniarr/{VERSION}"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, LOG_DIR, DEFINITIONS_DIR, CUSTOM_DEFINITIONS_DIR):
        d.mkdir(parents=True, exist_ok=True)
