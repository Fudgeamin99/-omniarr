"""Shows, episodes and movies: adding, refreshing, disk scans and 'what is wanted'."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import time
from pathlib import Path

from . import db, log, naming, parser, quality, settings
from .metadata import tmdb, tvmaze

L = log.get("library")


def _sort_title(t: str) -> str:
    t = t.lower()
    for a in ("the ", "a ", "an "):
        if t.startswith(a):
            return t[len(a):]
    return t


def today() -> str:
    return dt.date.today().isoformat()


def aired(ep: dict, grace_hours: float = 0) -> bool:
    if ep.get("airstamp"):
        try:
            ts = dt.datetime.fromisoformat(ep["airstamp"].replace("Z", "+00:00")).timestamp()
            return ts - grace_hours * 3600 <= time.time()
        except ValueError:
            pass
    return bool(ep.get("airdate")) and ep["airdate"] <= today()


# ------------------------------------------------------------------- series --
async def lookup_series(term: str) -> list[dict]:
    results = await tvmaze.search(term)
    have = {r["tvmaze_id"]: r["id"] for r in db.rows("SELECT id, tvmaze_id FROM series")}
    for r in results:
        r["kind"] = "tv"
        r["library_id"] = have.get(r["tvmaze_id"])
    return results


async def add_series(tvmaze_id: int, *, root: str | None = None, profile: str | None = None,
                     language_mode: str = "default", monitor: str = "all") -> dict:
    existing = db.row("SELECT * FROM series WHERE tvmaze_id = ?", tvmaze_id)
    if existing:
        return existing
    show = await tvmaze.show(tvmaze_id)
    eps = await tvmaze.episodes(tvmaze_id)
    aliases = await tvmaze.akas(tvmaze_id)
    folders = settings.get("folders")
    root = (root or folders["tv"]).rstrip("/")
    name_cfg = settings.get("naming")
    path = os.path.join(root, naming.series_folder(name_cfg["series_folder"], show["title"], show["year"]))
    sid = db.insert("series", {
        "tvmaze_id": tvmaze_id, "imdb_id": show.get("imdb_id"), "tvdb_id": show.get("tvdb_id"),
        "title": show["title"], "sort_title": _sort_title(show["title"]), "year": show["year"],
        "status": show["status"], "overview": show["overview"], "network": show["network"],
        "country": show["country"], "original_language": show["original_language"],
        "is_anime": 1 if show["is_anime"] else 0, "genres": db.jdump(show["genres"]),
        "aliases": db.jdump(aliases), "runtime": show["runtime"], "poster": show["poster"],
        "path": path, "root_folder": root,
        "quality_profile": profile or settings.get("quality")["default_profile"],
        "language_mode": language_mode or "default", "monitored": 0 if monitor == "none" else 1,
        "monitor_mode": monitor, "added_at": time.time(), "refreshed_at": time.time(),
    })
    _upsert_episodes(sid, eps, monitor, initial=True)
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        L.warning("Couldn't create %s: %s", path, e)
    scan_series(sid)
    L.info("Added show %s (%s episodes)", show["title"], len(eps))
    db.add_history("added", show["title"], "Added to library", kind="episode", series_id=sid)
    return db.row("SELECT * FROM series WHERE id = ?", sid)


def _upsert_episodes(sid: int, eps: list[dict], monitor: str, initial: bool = False) -> int:
    existing = {(r["season"], r["number"]): r for r in db.rows("SELECT * FROM episodes WHERE series_id = ?", sid)}
    added = 0
    for e in eps:
        key = (e["season"], e["number"])
        if key in existing:
            db.update("episodes", existing[key]["id"], {
                "tvmaze_id": e["tvmaze_id"], "title": e["title"], "airdate": e["airdate"],
                "airstamp": e["airstamp"], "overview": e["overview"], "runtime": e["runtime"],
                "absolute": e.get("absolute"),
            })
            continue
        if monitor == "none":
            mon = 0
        elif e["season"] == 0:
            mon = 0                      # specials off by default, like Sonarr
        elif monitor == "future" and initial:
            mon = 0 if aired(e) else 1
        else:
            mon = 1
        db.insert("episodes", {
            "series_id": sid, "tvmaze_id": e["tvmaze_id"], "season": e["season"], "number": e["number"],
            "absolute": e.get("absolute"), "title": e["title"], "airdate": e["airdate"], "airstamp": e["airstamp"],
            "overview": e["overview"], "runtime": e["runtime"], "monitored": mon,
        })
        added += 1
    return added


async def refresh_series(sid: int) -> int:
    s = db.row("SELECT * FROM series WHERE id = ?", sid)
    if not s:
        return 0
    show = await tvmaze.show(s["tvmaze_id"])
    eps = await tvmaze.episodes(s["tvmaze_id"])
    db.update("series", sid, {"title": show["title"], "status": show["status"], "overview": show["overview"],
                              "poster": show["poster"] or s["poster"], "network": show["network"],
                              "imdb_id": show.get("imdb_id") or s["imdb_id"], "runtime": show["runtime"],
                              "refreshed_at": time.time()})
    mode = "none" if not s["monitored"] else ("all" if s["monitor_mode"] != "none" else "none")
    added = _upsert_episodes(sid, eps, mode)
    if added:
        L.info("%s: %d new episode(s) announced", s["title"], added)
    return added


def delete_series(sid: int, delete_files: bool = False) -> None:
    s = db.row("SELECT * FROM series WHERE id = ?", sid)
    if not s:
        return
    with db.transaction():
        db.execute("DELETE FROM media_files WHERE series_id = ?", sid)
        db.execute("DELETE FROM episodes WHERE series_id = ?", sid)
        db.execute("DELETE FROM series WHERE id = ?", sid)
    if delete_files and s["path"] and os.path.isdir(s["path"]):
        shutil.rmtree(s["path"], ignore_errors=True)
    db.add_history("deleted", s["title"], "Removed from library" + (" and files deleted" if delete_files else ""),
                   kind="episode")


def episode_wanted(ep: dict, series: dict, file_quality: str | None = None) -> bool:
    if not series["monitored"] or not ep["monitored"]:
        return False
    if not aired(ep, grace_hours=0):
        return False
    if not ep.get("file_id"):
        return True
    prof = quality.profile(series["quality_profile"])
    return quality.tier(file_quality) < quality.tier(prof.cutoff)


def series_stats() -> dict[int, dict]:
    rows = db.rows("""
        SELECT e.series_id AS sid,
               SUM(CASE WHEN e.season > 0 THEN 1 ELSE 0 END) AS total,
               SUM(CASE WHEN e.file_id IS NOT NULL AND e.season > 0 THEN 1 ELSE 0 END) AS have,
               SUM(CASE WHEN e.monitored = 1 AND e.file_id IS NULL AND e.airdate IS NOT NULL AND e.airdate <= ?
                        THEN 1 ELSE 0 END) AS missing,
               SUM(CASE WHEN e.monitored = 1 AND e.season > 0 AND e.airdate IS NOT NULL AND e.airdate <= ?
                        THEN 1 ELSE 0 END) AS aired_monitored,
               MIN(CASE WHEN e.airdate > ? THEN e.airdate END) AS next_air
        FROM episodes e GROUP BY e.series_id""", today(), today(), today())
    sizes = {r["series_id"]: r for r in db.rows(
        "SELECT series_id, SUM(size) AS size, SUM(CASE WHEN sub_status = 'missing' THEN 1 ELSE 0 END) AS nosubs "
        "FROM media_files WHERE kind = 'episode' GROUP BY series_id")}
    out = {}
    for r in rows:
        sz = sizes.get(r["sid"], {})
        out[r["sid"]] = {"total": r["total"] or 0, "have": r["have"] or 0, "missing": r["missing"] or 0,
                         "aired_monitored": r["aired_monitored"] or 0, "next_air": r["next_air"],
                         "size": sz.get("size") or 0, "missing_subs": sz.get("nosubs") or 0}
    return out


# ------------------------------------------------------------------- movies --
async def lookup_movies(term: str) -> list[dict]:
    results = await tmdb.search(term)
    have = {r["tmdb_id"]: r["id"] for r in db.rows("SELECT id, tmdb_id FROM movies")}
    for r in results:
        r["kind"] = "movie"
        r["library_id"] = have.get(r["tmdb_id"])
    return results


async def add_movie(tmdb_id: int, *, root: str | None = None, profile: str | None = None,
                    language_mode: str = "default", monitored: bool = True) -> dict:
    existing = db.row("SELECT * FROM movies WHERE tmdb_id = ?", tmdb_id)
    if existing:
        return existing
    m = await tmdb.movie(tmdb_id)
    root = (root or settings.get("folders")["movies"]).rstrip("/")
    path = os.path.join(root, naming.movie_folder(settings.get("naming")["movie_folder"], m["title"], m["year"]))
    mid = db.insert("movies", {
        "tmdb_id": tmdb_id, "imdb_id": m.get("imdb_id"), "title": m["title"], "original_title": m.get("original_title"),
        "sort_title": _sort_title(m["title"]), "year": m["year"], "overview": m["overview"], "status": m.get("status"),
        "runtime": m.get("runtime"), "original_language": m.get("original_language"),
        "genres": db.jdump(m.get("genres") or []), "aliases": db.jdump(m.get("aliases") or []),
        "poster": m.get("poster"), "fanart": m.get("fanart"), "theatrical_date": m.get("theatrical_date"),
        "digital_date": m.get("digital_date"), "physical_date": m.get("physical_date"), "path": path,
        "root_folder": root, "quality_profile": profile or settings.get("quality")["default_profile"],
        "language_mode": language_mode or "default", "monitored": 1 if monitored else 0,
        "added_at": time.time(), "refreshed_at": time.time(),
    })
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        L.warning("Couldn't create %s: %s", path, e)
    scan_movie(mid)
    L.info("Added movie %s (%s)", m["title"], m["year"])
    db.add_history("added", f"{m['title']} ({m['year']})", "Added to library", kind="movie", movie_id=mid)
    return db.row("SELECT * FROM movies WHERE id = ?", mid)


async def refresh_movie(mid: int) -> None:
    mv = db.row("SELECT * FROM movies WHERE id = ?", mid)
    if not mv:
        return
    m = await tmdb.movie(mv["tmdb_id"])
    db.update("movies", mid, {"status": m.get("status"), "overview": m["overview"], "poster": m.get("poster") or
                              mv["poster"], "digital_date": m.get("digital_date"),
                              "physical_date": m.get("physical_date"), "theatrical_date": m.get("theatrical_date"),
                              "runtime": m.get("runtime"), "imdb_id": m.get("imdb_id") or mv["imdb_id"],
                              "refreshed_at": time.time()})


def delete_movie(mid: int, delete_files: bool = False) -> None:
    mv = db.row("SELECT * FROM movies WHERE id = ?", mid)
    if not mv:
        return
    with db.transaction():
        db.execute("DELETE FROM media_files WHERE movie_id = ?", mid)
        db.execute("DELETE FROM movies WHERE id = ?", mid)
    if delete_files and mv["path"] and os.path.isdir(mv["path"]):
        shutil.rmtree(mv["path"], ignore_errors=True)
    db.add_history("deleted", mv["title"], "Removed from library" + (" and files deleted" if delete_files else ""),
                   kind="movie")


def movie_available(mv: dict) -> bool:
    return tmdb.is_available(mv)


def movie_wanted(mv: dict, file_quality: str | None = None) -> bool:
    if not mv["monitored"] or not movie_available(mv):
        return False
    if not mv.get("file_id"):
        return True
    return quality.tier(file_quality) < quality.tier(quality.profile(mv["quality_profile"]).cutoff)


# --------------------------------------------------------------- disk scans --
def _videos(folder: str) -> list[Path]:
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for root, _dirs, files in os.walk(folder):
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in parser.VIDEO_EXTS and "sample" not in p.stem.lower():
                out.append(p)
    return out


def scan_series(sid: int) -> int:
    """Link video files already in the show's folder to episodes."""
    s = db.row("SELECT * FROM series WHERE id = ?", sid)
    if not s:
        return 0
    eps = {(e["season"], e["number"]): e for e in db.rows("SELECT * FROM episodes WHERE series_id = ?", sid)}
    by_abs = {e["absolute"]: e for e in eps.values() if e.get("absolute")}
    known = {r["path"] for r in db.rows("SELECT path FROM media_files WHERE series_id = ?", sid)}
    found = 0
    for p in _videos(s["path"]):
        if str(p) in known:
            continue
        pr = parser.parse(p.name)
        targets = []
        if pr.seasons and pr.episodes:
            targets = [eps.get((pr.seasons[0], n)) for n in pr.episodes]
        elif pr.absolute:
            targets = [by_abs.get(n) for n in pr.absolute]
        targets = [t for t in targets if t and not t.get("file_id")]
        if not targets:
            continue
        fid = db.insert("media_files", {
            "kind": "episode", "series_id": sid, "path": str(p), "size": p.stat().st_size,
            "quality": pr.quality, "revision": pr.revision, "release_group": pr.group, "release_title": p.stem,
            "added_at": time.time(), "sub_status": "pending"})
        for t in targets:
            db.update("episodes", t["id"], {"file_id": fid})
            t["file_id"] = fid
        found += 1
    # forget files that vanished from disk
    for f in db.rows("SELECT id, path FROM media_files WHERE series_id = ?", sid):
        if not os.path.exists(f["path"]):
            db.execute("UPDATE episodes SET file_id = NULL WHERE file_id = ?", f["id"])
            db.execute("DELETE FROM media_files WHERE id = ?", f["id"])
    return found


def scan_movie(mid: int) -> int:
    mv = db.row("SELECT * FROM movies WHERE id = ?", mid)
    if not mv:
        return 0
    if mv.get("file_id"):
        f = db.row("SELECT * FROM media_files WHERE id = ?", mv["file_id"])
        if f and os.path.exists(f["path"]):
            return 0
        db.update("movies", mid, {"file_id": None})
        if f:
            db.execute("DELETE FROM media_files WHERE id = ?", f["id"])
    vids = sorted(_videos(mv["path"]), key=lambda p: p.stat().st_size, reverse=True)
    if not vids:
        return 0
    p = vids[0]
    pr = parser.parse(p.name)
    fid = db.insert("media_files", {
        "kind": "movie", "movie_id": mid, "path": str(p), "size": p.stat().st_size, "quality": pr.quality,
        "revision": pr.revision, "release_group": pr.group, "release_title": p.stem, "added_at": time.time(),
        "sub_status": "pending"})
    db.update("movies", mid, {"file_id": fid})
    return 1
