"""TMDB: movie metadata (free API key)."""
from __future__ import annotations

import datetime as dt

from .. import config, net, settings


class TMDBKeyMissing(Exception):
    pass


def _auth() -> tuple[dict, dict]:
    key = (settings.get("metadata").get("tmdb_key") or "").strip()
    if not key:
        raise TMDBKeyMissing("Add your free TMDB key in Connections to search movies.")
    if len(key) > 40:   # v4 read-access token
        return {}, {"Authorization": f"Bearer {key}"}
    return {"api_key": key}, {}


async def _get(path: str, params: dict | None = None, key: str | None = None):
    if key:
        p, h = ({}, {"Authorization": f"Bearer {key}"}) if len(key) > 40 else ({"api_key": key}, {})
    else:
        p, h = _auth()
    p.update(params or {})
    return await net.get_json(f"{config.TMDB_URL}{path}", params=p, headers=h)


def _img(path: str | None, size: str = "w500") -> str | None:
    return f"{config.TMDB_IMAGE_URL}/{size}{path}" if path else None


def _summary(m: dict) -> dict:
    rd = m.get("release_date") or ""
    return {
        "tmdb_id": m["id"],
        "title": m.get("title") or m.get("original_title") or "",
        "original_title": m.get("original_title"),
        "year": int(rd[:4]) if rd[:4].isdigit() else None,
        "overview": m.get("overview") or "",
        "poster": _img(m.get("poster_path")),
        "poster_small": _img(m.get("poster_path"), "w185"),
        "original_language": m.get("original_language") or "en",
        "theatrical_date": rd or None,
    }


async def test_key(key: str) -> bool:
    await _get("/configuration", key=key)
    return True


async def search(term: str) -> list[dict]:
    year = None
    parts = term.rsplit(" ", 1)
    params = {"query": term, "include_adult": "false"}
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
        params = {"query": parts[0], "year": parts[1], "include_adult": "false"}
        year = parts[1]
    data = await _get("/search/movie", params)
    results = [_summary(m) for m in data.get("results", [])]
    if year and not results:
        data = await _get("/search/movie", {"query": term, "include_adult": "false"})
        results = [_summary(m) for m in data.get("results", [])]
    return results


async def movie(tmdb_id: int) -> dict:
    m = await _get(f"/movie/{tmdb_id}", {"append_to_response": "release_dates,alternative_titles"})
    out = _summary(m)
    out.update({
        "imdb_id": m.get("imdb_id"),
        "status": m.get("status"),
        "runtime": m.get("runtime"),
        "genres": [g["name"] for g in m.get("genres") or []],
        "fanart": _img(m.get("backdrop_path"), "w1280"),
    })
    digital = physical = theatrical = None
    for country in (m.get("release_dates") or {}).get("results", []):
        for r in country.get("release_dates", []):
            d = (r.get("release_date") or "")[:10]
            if not d:
                continue
            t = r.get("type")
            if t == 4 and (digital is None or d < digital):
                digital = d
            elif t == 5 and (physical is None or d < physical):
                physical = d
            elif t in (2, 3) and (theatrical is None or d < theatrical):
                theatrical = d
    out["digital_date"] = digital
    out["physical_date"] = physical
    out["theatrical_date"] = theatrical or out.get("theatrical_date")
    alts = (m.get("alternative_titles") or {}).get("titles", [])
    out["aliases"] = [a["title"] for a in alts if a.get("iso_3166_1") in ("US", "GB", "CA", "AU")][:10]
    return out


def is_available(mv: dict, today: dt.date | None = None) -> bool:
    """Released for home viewing: digital/physical date passed, or 90 days after cinema release."""
    today = today or dt.date.today()
    for key in ("digital_date", "physical_date"):
        d = mv.get(key)
        if d and d <= today.isoformat():
            return True
    th = mv.get("theatrical_date")
    if th:
        try:
            return dt.date.fromisoformat(th[:10]) + dt.timedelta(days=90) <= today
        except ValueError:
            return False
    return (mv.get("status") or "") == "Released" and bool(mv.get("year")) and mv["year"] < today.year
