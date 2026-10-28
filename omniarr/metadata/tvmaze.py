"""TVMaze: free TV metadata, no key needed."""
from __future__ import annotations

import asyncio
import html
import re

from .. import config, net

_sem = asyncio.Semaphore(4)   # TVMaze allows ~20 calls / 10 s


def _text(s: str | None) -> str:
    if not s:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _lang(name: str | None) -> str:
    table = {"english": "en", "japanese": "ja", "korean": "ko", "chinese": "zh", "spanish": "es",
             "french": "fr", "german": "de", "italian": "it", "portuguese": "pt", "russian": "ru",
             "hindi": "hi", "swedish": "sv", "danish": "da", "norwegian": "no", "dutch": "nl", "turkish": "tr",
             "thai": "th", "polish": "pl", "finnish": "fi", "icelandic": "is", "hebrew": "he", "arabic": "ar"}
    return table.get((name or "").lower(), (name or "en")[:2].lower())


def _show(s: dict) -> dict:
    img = s.get("image") or {}
    net_ = s.get("network") or s.get("webChannel") or {}
    country = ((net_.get("country") or {}).get("code") or "") if isinstance(net_, dict) else ""
    premiered = s.get("premiered") or ""
    lang = _lang(s.get("language"))
    genres = s.get("genres") or []
    is_anime = (s.get("type") == "Animation" and lang == "ja") or ("Anime" in genres)
    ext = s.get("externals") or {}
    return {
        "tvmaze_id": s["id"],
        "title": s.get("name") or "",
        "year": int(premiered[:4]) if premiered[:4].isdigit() else None,
        "status": s.get("status") or "",
        "overview": _text(s.get("summary")),
        "network": net_.get("name") if isinstance(net_, dict) else None,
        "country": country,
        "original_language": lang,
        "is_anime": bool(is_anime),
        "genres": genres,
        "runtime": s.get("averageRuntime") or s.get("runtime"),
        "poster": img.get("original") or img.get("medium"),
        "poster_small": img.get("medium") or img.get("original"),
        "imdb_id": ext.get("imdb"),
        "tvdb_id": ext.get("thetvdb"),
        "premiered": premiered,
    }


async def _get(path: str, params: dict | None = None):
    async with _sem:
        for attempt in range(3):
            try:
                return await net.get_json(f"{config.TVMAZE_URL}{path}", params=params)
            except Exception as e:  # 429 → back off
                if "429" in str(e) and attempt < 2:
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                raise


async def search(term: str) -> list[dict]:
    data = await _get("/search/shows", {"q": term})
    return [_show(item["show"]) for item in data or []]


async def show(tvmaze_id: int) -> dict:
    data = await _get(f"/shows/{tvmaze_id}")
    return _show(data)


async def akas(tvmaze_id: int) -> list[str]:
    try:
        data = await _get(f"/shows/{tvmaze_id}/akas")
    except Exception:
        return []
    out = []
    for a in data or []:
        country = (a.get("country") or {}).get("code") if a.get("country") else None
        if a.get("name") and (country in (None, "US", "GB", "CA", "AU", "NZ", "IE") or a["name"].isascii()):
            out.append(a["name"])
    return out


async def episodes(tvmaze_id: int) -> list[dict]:
    data = await _get(f"/shows/{tvmaze_id}/episodes", {"specials": 1})
    regular, specials = [], []
    for e in data or []:
        item = {
            "tvmaze_id": e["id"],
            "season": e.get("season") or 0,
            "number": e.get("number"),
            "title": e.get("name") or "",
            "airdate": e.get("airdate") or None,
            "airstamp": e.get("airstamp") or None,
            "overview": _text(e.get("summary")),
            "runtime": e.get("runtime"),
        }
        if item["number"] is None or e.get("type") in ("significant_special", "insignificant_special"):
            specials.append(item)
        else:
            regular.append(item)
    # Specials live in Season 00, numbered in air order (what Plex/Jellyfin expect)
    specials.sort(key=lambda x: (x["airdate"] or "9999", x["tvmaze_id"]))
    for i, sp in enumerate(specials, 1):
        sp["season"], sp["number"] = 0, i
    regular.sort(key=lambda x: (x["season"], x["number"]))
    for i, ep in enumerate(regular, 1):
        ep["absolute"] = i
    # de-duplicate (season, number) clashes defensively
    seen, out = set(), []
    for ep in regular + specials:
        key = (ep["season"], ep["number"])
        if key in seen:
            continue
        seen.add(key)
        out.append(ep)
    return out
