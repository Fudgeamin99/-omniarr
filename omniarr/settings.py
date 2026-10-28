"""User settings, stored as JSON per section in the settings table."""
import copy
from typing import Any

from . import db

DEFAULTS: dict[str, dict[str, Any]] = {
    "qbittorrent": {
        "url": "http://qbittorrent:8080",
        "username": "admin",
        "password": "",
        "category": "omniarr",
        "path_mappings": [],            # [{"remote": "D:\\Downloads\\", "local": "/data/torrents/"}]
        "remove_completed": True,       # remove torrent once imported AND seeding goals met
        "linked": False,
    },
    "folders": {
        "tv": "/data/media/tv",
        "movies": "/data/media/movies",
        "use_hardlinks": True,
    },
    "quality": {
        "default_profile": "hd-1080p",
    },
    "language": {
        "mode": "english",              # english | any
        "anime_prefer_dub": True,
    },
    "metadata": {
        "tmdb_key": "",
    },
    "naming": {
        "rename": True,
        "series_folder": "{Show Title} ({Year})",
        "season_folder": "Season {season:00}",
        "episode": "{Show Title} - S{season:00}E{episode:00} - {Episode Title} [{Quality}]",
        "movie_folder": "{Movie Title} ({Year})",
        "movie_file": "{Movie Title} ({Year}) [{Quality}]",
    },
    "subtitles": {
        "enabled": True,
        "languages": ["en"],
        "mode": "normal_hi_fallback",   # normal_hi_fallback | normal | hi | forced
        "skip_embedded": True,
        "retry": True,
        "upgrade": False,
        "providers": [
            {"id": "opensubtitles", "enabled": True, "api_key": "", "username": "", "password": ""},
            {"id": "podnapisi", "enabled": True},
        ],
    },
    "unblock": {
        "flaresolverr_url": "http://flaresolverr:8191",
        "min_interval": 2.0,
        "daily_limits": True,
        "backoff": True,
        "cache_minutes": 30,
        "proxy_url": "",
        "detect_isp_block": True,
    },
    "search": {
        "min_seeders": 1,
        "ignored_words": [],
        "reject_hardcoded_subs": True,
        "stall_hours": 6,
        "auto_research_failed": True,
        "rss_enabled": True,
    },
    "security": {
        "enabled": False,
        "username": "",
        "password_hash": "",
        "secret": "",
    },
    "general": {
        "setup_dismissed": False,
        "defaults_seeded": False,
    },
}

# Keys never sent to the browser in clear text.
SECRET_FIELDS = {("qbittorrent", "password"), ("metadata", "tmdb_key"), ("security", "password_hash"),
                 ("security", "secret")}
MASK = "••••••••"


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for k, v in override.items():
            out[k] = _merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(override) if override is not None else copy.deepcopy(base)


def get(section: str) -> dict:
    stored = db.jload(db.scalar("SELECT value FROM settings WHERE key = ?", section), {})
    return _merge(DEFAULTS.get(section, {}), stored)


def all_settings() -> dict:
    return {k: get(k) for k in DEFAULTS}


def save(section: str, values: dict) -> dict:
    current = get(section)
    merged = _merge(current, values)
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        section, db.jdump(merged),
    )
    return merged


def public() -> dict:
    """Settings for the UI with secrets masked."""
    data = all_settings()
    for section, key in SECRET_FIELDS:
        if data.get(section, {}).get(key):
            data[section][key] = MASK
    for p in data["subtitles"]["providers"]:
        for key in ("password", "api_key"):
            if p.get(key):
                p[key] = MASK
    data["security"].pop("secret", None)
    return data


def strip_masks(section: str, values: dict) -> dict:
    """Drop masked values coming back from the UI so secrets are not overwritten."""
    out = {}
    for k, v in values.items():
        if v == MASK:
            continue
        out[k] = v
    if section == "subtitles" and isinstance(out.get("providers"), list):
        current = {p["id"]: p for p in get("subtitles")["providers"]}
        provs = []
        for p in out["providers"]:
            cur = current.get(p.get("id"), {})
            provs.append({k: (cur.get(k, "") if v == MASK else v) for k, v in p.items()})
        out["providers"] = provs
    return out
