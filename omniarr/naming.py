"""Folder and file naming from user patterns."""
from __future__ import annotations

import re

TOKENS = ["{Show Title}", "{Year}", "{season:00}", "{episode:00}", "{Episode Title}", "{Quality}",
          "{Movie Title}", "{Air Date}", "{Release Group}"]

_ILLEGAL = re.compile(r'[<>"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    name = name.replace(":", " -").replace("…", "...")
    name = _ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name)
    # tidy leftovers from empty tokens: "[]", "()", " - - ", trailing separators
    name = re.sub(r"\[\s*\]|\(\s*\)|\{\s*\}", "", name)
    name = re.sub(r"(\s-\s*){2,}", " - ", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    return name[:200].rstrip(" .") or "Unknown"


def _fill(pattern: str, values: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1).strip()
        fmt = m.group(2)
        val = values.get(key.lower())
        if val is None:
            return ""
        if fmt and isinstance(val, int):
            return str(val).zfill(len(fmt))
        if fmt and isinstance(val, str) and val.isdigit():
            return val.zfill(len(fmt))
        return str(val)
    return re.sub(r"\{([A-Za-z ]+?)(?::(0+))?\}", repl, pattern)


def series_folder(pattern: str, title: str, year: int | None) -> str:
    return sanitize(_fill(pattern, {"show title": title, "series title": title, "year": year or ""}))


def season_folder(pattern: str, season: int) -> str:
    if season == 0 and "{season" not in pattern.lower():
        return "Specials"
    return sanitize(_fill(pattern, {"season": season}))


def episode_file(pattern: str, show: str, year: int | None, season: int, episodes: list[dict],
                 quality: str, group: str | None = None) -> str:
    """episodes: [{"number": 1, "title": "...", "airdate": "..."}] — several for multi-episode files."""
    eps = sorted(episodes, key=lambda e: e["number"])
    first = eps[0]
    ep_value: str | int = first["number"]
    if len(eps) > 1:
        ep_value = f"{eps[0]['number']:02d}-E{eps[-1]['number']:02d}"
    titles = [e.get("title") or "" for e in eps if e.get("title")]
    ep_title = titles[0] if len(set(titles)) == 1 else " + ".join(titles)
    if re.fullmatch(r"(?i)(episode|chapter)\s*\d+|tba|tbd", ep_title or ""):
        ep_title = ""
    vals = {"show title": show, "series title": show, "year": year or "", "season": season,
            "episode": ep_value, "episode title": ep_title, "quality": quality if quality != "Unknown" else "",
            "air date": first.get("airdate") or "", "release group": group or ""}
    return sanitize(_fill(pattern, vals))


def movie_folder(pattern: str, title: str, year: int | None) -> str:
    return sanitize(_fill(pattern, {"movie title": title, "year": year or ""}))


def movie_file(pattern: str, title: str, year: int | None, quality: str, group: str | None = None) -> str:
    return sanitize(_fill(pattern, {"movie title": title, "year": year or "",
                                    "quality": quality if quality != "Unknown" else "",
                                    "release group": group or ""}))


def preview(cfg: dict) -> dict:
    show, year = "Glass Harbor", 2025
    sf = series_folder(cfg["series_folder"], show, year)
    def ep(season, n, title):
        return episode_file(cfg["episode"], show, year, season, [{"number": n, "title": title}], "WEBDL-1080p")
    return {
        "series_folder": sf,
        "season_folder": season_folder(cfg["season_folder"], 1),
        "specials_folder": season_folder(cfg["season_folder"], 0),
        "episode": ep(1, 1, "Return to Port"),
        "episode2": ep(1, 2, "Dry Dock"),
        "multi": episode_file(cfg["episode"], show, year, 1, [{"number": 3, "title": "Part One"},
                                                              {"number": 4, "title": "Part Two"}], "WEBDL-1080p"),
        "movie_folder": movie_folder(cfg["movie_folder"], "Northbound", 2025),
        "movie_file": movie_file(cfg["movie_file"], "Northbound", 2025, "WEBDL-2160p"),
    }
