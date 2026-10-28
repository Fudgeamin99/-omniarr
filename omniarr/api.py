"""JSON API used by the web UI."""
from __future__ import annotations

import asyncio
import datetime as dt
import functools
import hashlib
import hmac
import os
import secrets
import shutil
import time
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import (config, db, downloads, library, log, naming, net, qbittorrent, quality, scheduler, search,
               settings)
from .indexers import catalog, defaults, manager
from .metadata import tmdb
from .subtitles import manager as subs

L = log.get("api")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def endpoint(fn):
    @functools.wraps(fn)
    async def wrapper(request: Request):
        try:
            res = await fn(request)
            if isinstance(res, JSONResponse):
                return res
            return JSONResponse(res if res is not None else {"ok": True})
        except ApiError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)
        except tmdb.TMDBKeyMissing as e:
            return JSONResponse({"error": str(e), "code": "tmdb_key"}, status_code=400)
        except (qbittorrent.QbitError, net.IndexerError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            L.exception("API error in %s", fn.__name__)
            return JSONResponse({"error": f"Something went wrong: {e}"}, status_code=500)
    return wrapper


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _int(request: Request, name: str) -> int:
    try:
        return int(request.path_params[name])
    except (KeyError, ValueError) as e:
        raise ApiError("Bad id") from e


# --------------------------------------------------------------------- auth --
def _secret() -> str:
    sec = settings.get("security")
    if not sec.get("secret"):
        sec = settings.save("security", {"secret": secrets.token_hex(32)})
    return sec["secret"]


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(calc, h)


def session_ok(request: Request) -> bool:
    sec = settings.get("security")
    if not sec.get("enabled"):
        return True
    tok = request.cookies.get("omniarr_session")
    if not tok:
        return False
    try:
        URLSafeTimedSerializer(_secret()).loads(tok, max_age=30 * 86400)
        return True
    except BadSignature:
        return False


@endpoint
async def auth_login(request: Request):
    data = await body(request)
    sec = settings.get("security")
    if not sec.get("enabled"):
        return {"ok": True}
    if data.get("username") != sec.get("username") or not check_password(data.get("password") or "",
                                                                          sec.get("password_hash") or ""):
        await asyncio.sleep(0.5)
        raise ApiError("Wrong username or password", 401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("omniarr_session", URLSafeTimedSerializer(_secret()).dumps({"u": sec["username"]}),
                    max_age=30 * 86400, httponly=True, samesite="lax")
    return resp


@endpoint
async def auth_logout(request: Request):
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("omniarr_session")
    return resp


@endpoint
async def auth_status(request: Request):
    return {"enabled": settings.get("security").get("enabled"), "logged_in": session_ok(request)}


# ------------------------------------------------------------------- status --
_qbit_state: dict[str, Any] = {"ok": None, "checked": 0, "message": "", "version": None}


async def qbit_status(force: bool = False) -> dict:
    if not force and time.time() - _qbit_state["checked"] < 60:
        return _qbit_state
    cfg = settings.get("qbittorrent")
    if not cfg.get("linked"):
        _qbit_state.update(ok=None, message="Not linked yet", checked=time.time())
        return _qbit_state
    try:
        v = await qbittorrent.get_client().version()
        _qbit_state.update(ok=True, version=v, message=f"Connected · {v}", checked=time.time())
    except Exception as e:
        _qbit_state.update(ok=False, message=str(e), checked=time.time())
    return _qbit_state


def _folder_state(path: str) -> dict:
    exists = os.path.isdir(path)
    writable = exists and os.access(path, os.W_OK)
    free = None
    if exists:
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            pass
    return {"path": path, "exists": exists, "writable": writable, "free": free}


@endpoint
async def status(request: Request):
    folders = settings.get("folders")
    q = await qbit_status()
    idx = db.rows("SELECT * FROM indexers")
    enabled = [i for i in idx if i["enabled"]]
    healthy = [i for i in enabled if manager.health(i)["state"] in ("ok", "new")]
    tv, mv = _folder_state(folders["tv"]), _folder_state(folders["movies"])
    tmdb_set = bool(settings.get("metadata").get("tmdb_key"))
    steps = [
        {"id": "folders", "label": "Library folders", "done": tv["writable"] and mv["writable"]},
        {"id": "indexers", "label": "Indexers", "done": bool(enabled)},
        {"id": "qbittorrent", "label": "Link qBittorrent", "done": bool(q.get("ok"))},
        {"id": "tmdb", "label": "Movie info key", "done": tmdb_set},
    ]
    return {
        "version": config.VERSION,
        "setup": steps,
        "setup_complete": all(s["done"] for s in steps),
        "setup_dismissed": settings.get("general").get("setup_dismissed"),
        "qbittorrent": q,
        "indexers": {"total": len(idx), "enabled": len(enabled), "healthy": len(healthy)},
        "folders": {"tv": tv, "movies": mv},
        "tmdb": tmdb_set,
        "jobs": [j for j in search.jobs.list() if j["state"] in ("queued", "running")][:10],
    }


@endpoint
async def dashboard(request: Request):
    stats = library.series_stats()
    series_n = db.scalar("SELECT COUNT(*) FROM series") or 0
    movies_n = db.scalar("SELECT COUNT(*) FROM movies") or 0
    missing_eps = sum(s["missing"] for s in stats.values())
    missing_movies = sum(1 for m in db.rows("SELECT * FROM movies WHERE monitored = 1 AND file_id IS NULL")
                         if library.movie_available(m))
    q = downloads.queue()
    return {"counts": {"shows": series_n, "movies": movies_n, "missing": missing_eps + missing_movies,
                       "downloading": len([x for x in q if x["status"] == "downloading"])},
            "queue": q[:6], "upcoming": _calendar(8)[:8],
            "recent": _history(limit=8)}


# ----------------------------------------------------------------- library --
def _series_item(s: dict, st: dict, queued: set) -> dict:
    total = st.get("aired_monitored") or 0
    have = st.get("have") or 0
    missing = st.get("missing") or 0
    downloading = s["id"] in queued
    if downloading:
        state = "downloading"
    elif missing:
        state = "missing"
    elif total and have >= total:
        state = "complete"
    else:
        state = "ok"
    return {"id": s["id"], "kind": "tv", "title": s["title"], "year": s["year"], "poster": s["poster"],
            "status": s["status"], "network": s["network"], "monitored": bool(s["monitored"]),
            "have": have, "total": st.get("total") or 0, "aired": total, "missing": missing, "state": state,
            "next_air": st.get("next_air"), "size": st.get("size") or 0, "missing_subs": st.get("missing_subs") or 0,
            "added_at": s["added_at"], "sort_title": s["sort_title"]}


def _movie_item(m: dict, queued: set, files: dict) -> dict:
    f = files.get(m["file_id"]) if m["file_id"] else None
    avail = library.movie_available(m)
    if m["id"] in queued:
        state = "downloading"
    elif f:
        state = "complete"
    elif not avail:
        state = "unreleased"
    else:
        state = "missing"
    return {"id": m["id"], "kind": "movie", "title": m["title"], "year": m["year"], "poster": m["poster"],
            "monitored": bool(m["monitored"]), "state": state, "quality": f["quality"] if f else None,
            "size": f["size"] if f else 0, "missing_subs": 1 if f and f["sub_status"] == "missing" else 0,
            "available": avail, "added_at": m["added_at"], "sort_title": m["sort_title"],
            "digital_date": m["digital_date"], "theatrical_date": m["theatrical_date"]}


def _queued_sets() -> tuple[set, set]:
    rows = db.rows("SELECT series_id, movie_id FROM downloads WHERE status IN ('downloading','importing')")
    return {r["series_id"] for r in rows if r["series_id"]}, {r["movie_id"] for r in rows if r["movie_id"]}


@endpoint
async def library_list(request: Request):
    stats = library.series_stats()
    qs, qm = _queued_sets()
    files = {f["id"]: f for f in db.rows("SELECT id, quality, size, sub_status FROM media_files WHERE kind='movie'")}
    items = [_series_item(s, stats.get(s["id"], {}), qs) for s in db.rows("SELECT * FROM series")]
    items += [_movie_item(m, qm, files) for m in db.rows("SELECT * FROM movies")]
    return {"items": items}


def _file_info(fid: int | None, files: dict) -> dict | None:
    f = files.get(fid) if fid else None
    if not f:
        return None
    return {"id": f["id"], "path": f["path"], "quality": f["quality"], "size": f["size"],
            "sub_status": f["sub_status"], "subtitles": db.jload(f["subtitles"], {}),
            "release_group": f["release_group"]}


@endpoint
async def series_detail(request: Request):
    sid = _int(request, "id")
    s = db.row("SELECT * FROM series WHERE id = ?", sid)
    if not s:
        raise ApiError("Show not found", 404)
    eps = db.rows("SELECT * FROM episodes WHERE series_id = ? ORDER BY season DESC, number DESC", sid)
    files = {f["id"]: f for f in db.rows("SELECT * FROM media_files WHERE series_id = ?", sid)}
    q = db.rows("SELECT * FROM downloads WHERE series_id = ? AND status IN ('downloading','importing',"
                "'import_blocked')", sid)
    q_by_ep: dict[int, dict] = {}
    for d in q:
        for eid in db.jload(d["episode_ids"], []):
            q_by_ep[eid] = {"progress": d["progress"], "status": d["status"], "message": d["message"]}
    seasons: dict[int, dict] = {}
    for e in eps:
        fi = _file_info(e["file_id"], files)
        aired = library.aired(e)
        if e["id"] in q_by_ep:
            state = "downloading"
        elif fi:
            state = "have"
        elif not aired:
            state = "upcoming"
        elif e["monitored"]:
            state = "missing"
        else:
            state = "unmonitored"
        item = {"id": e["id"], "season": e["season"], "number": e["number"], "absolute": e["absolute"],
                "title": e["title"], "airdate": e["airdate"], "overview": e["overview"],
                "monitored": bool(e["monitored"]), "state": state, "file": fi, "queue": q_by_ep.get(e["id"])}
        sn = seasons.setdefault(e["season"], {"season": e["season"], "episodes": [], "have": 0, "aired": 0,
                                              "monitored": False})
        sn["episodes"].append(item)
        if e["monitored"]:
            sn["monitored"] = True
        if aired and (e["monitored"] or fi):
            sn["aired"] += 1
            if fi:
                sn["have"] += 1
    st = library.series_stats().get(sid, {})
    out = dict(s)
    out["genres"] = db.jload(s["genres"], [])
    out["aliases"] = db.jload(s["aliases"], [])
    out["stats"] = st
    out["missing_subs"] = sum(1 for f in files.values() if f["sub_status"] == "missing")
    out["seasons"] = sorted(seasons.values(), key=lambda x: (x["season"] == 0, -x["season"]))
    out["profile_name"] = quality.profile(s["quality_profile"]).name
    return out


@endpoint
async def series_update(request: Request):
    sid = _int(request, "id")
    data = await body(request)
    allowed = {k: data[k] for k in ("quality_profile", "language_mode", "monitored") if k in data}
    if "monitored" in allowed:
        allowed["monitored"] = 1 if allowed["monitored"] else 0
    if "path" in data and data["path"]:
        allowed["path"] = data["path"]
    db.update("series", sid, allowed)
    return {"ok": True}


@endpoint
async def series_delete(request: Request):
    sid = _int(request, "id")
    library.delete_series(sid, delete_files=request.query_params.get("delete_files") == "true")
    return {"ok": True}


@endpoint
async def series_add(request: Request):
    data = await body(request)
    if not data.get("tvmaze_id"):
        raise ApiError("Missing show id")
    s = await library.add_series(int(data["tvmaze_id"]), root=data.get("root"), profile=data.get("quality_profile"),
                                 language_mode=data.get("language_mode") or "default",
                                 monitor=data.get("monitor") or "all")
    if data.get("search", True) and data.get("monitor") != "none":
        search.jobs.enqueue(f"series:{s['id']}", f"Searching {s['title']}",
                            lambda sid=s["id"]: search.search_series_missing(sid))
    return {"id": s["id"], "kind": "tv"}


@endpoint
async def series_refresh(request: Request):
    sid = _int(request, "id")
    n = await library.refresh_series(sid)
    found = library.scan_series(sid)
    return {"new_episodes": n, "files_found": found}


@endpoint
async def series_search(request: Request):
    sid = _int(request, "id")
    s = db.row("SELECT title FROM series WHERE id = ?", sid)
    season = request.query_params.get("season")
    if season is not None:
        job = search.jobs.enqueue(f"season:{sid}:{season}", f"Searching {s['title']} season {season}",
                                  lambda: search.search_season(sid, int(season)))
    else:
        job = search.jobs.enqueue(f"series:{sid}", f"Searching {s['title']}",
                                  lambda: search.search_series_missing(sid))
    return job


@endpoint
async def series_subtitles(request: Request):
    sid = _int(request, "id")
    s = db.row("SELECT title FROM series WHERE id = ?", sid)
    return search.jobs.enqueue(f"subs:{sid}", f"Finding subtitles for {s['title']}",
                               lambda: subs.search_series(sid))


@endpoint
async def episode_update(request: Request):
    eid = _int(request, "id")
    data = await body(request)
    if "monitored" in data:
        db.update("episodes", eid, {"monitored": 1 if data["monitored"] else 0})
    return {"ok": True}


@endpoint
async def season_monitor(request: Request):
    sid = _int(request, "id")
    data = await body(request)
    db.execute("UPDATE episodes SET monitored = ? WHERE series_id = ? AND season = ?",
               1 if data.get("monitored") else 0, sid, int(data.get("season", -1)))
    return {"ok": True}


@endpoint
async def episode_search(request: Request):
    eid = _int(request, "id")
    e = db.row("SELECT e.*, s.title AS show FROM episodes e JOIN series s ON s.id = e.series_id WHERE e.id = ?", eid)
    return search.jobs.enqueue(f"episode:{eid}", f"Searching {e['show']} S{e['season']:02d}E{e['number']:02d}",
                               lambda: search.search_episode(eid))


@endpoint
async def episode_releases(request: Request):
    return await search.interactive_episode(_int(request, "id"))


@endpoint
async def season_releases(request: Request):
    return await search.interactive_season(_int(request, "id"), int(request.query_params.get("season", 1)))


@endpoint
async def file_subtitles(request: Request):
    fid = _int(request, "id")
    return {"result": await subs.process_file(fid, force=True)}


# ------------------------------------------------------------------- movies --
@endpoint
async def movie_detail(request: Request):
    mid = _int(request, "id")
    m = db.row("SELECT * FROM movies WHERE id = ?", mid)
    if not m:
        raise ApiError("Movie not found", 404)
    files = {f["id"]: f for f in db.rows("SELECT * FROM media_files WHERE movie_id = ?", mid)}
    qd = db.row("SELECT * FROM downloads WHERE movie_id = ? AND status IN ('downloading','importing',"
                "'import_blocked') ORDER BY added_at DESC", mid)
    out = dict(m)
    out["genres"] = db.jload(m["genres"], [])
    out["file"] = _file_info(m["file_id"], files)
    out["available"] = library.movie_available(m)
    out["queue"] = {"progress": qd["progress"], "status": qd["status"], "message": qd["message"]} if qd else None
    out["profile_name"] = quality.profile(m["quality_profile"]).name
    return out


@endpoint
async def movie_add(request: Request):
    data = await body(request)
    if not data.get("tmdb_id"):
        raise ApiError("Missing movie id")
    m = await library.add_movie(int(data["tmdb_id"]), root=data.get("root"), profile=data.get("quality_profile"),
                                language_mode=data.get("language_mode") or "default",
                                monitored=data.get("monitored", True))
    if data.get("search", True) and m["monitored"]:
        search.jobs.enqueue(f"movie:{m['id']}", f"Searching {m['title']}", lambda mid=m["id"]: search.search_movie(mid))
    return {"id": m["id"], "kind": "movie"}


@endpoint
async def movie_update(request: Request):
    mid = _int(request, "id")
    data = await body(request)
    allowed = {k: data[k] for k in ("quality_profile", "language_mode", "monitored") if k in data}
    if "monitored" in allowed:
        allowed["monitored"] = 1 if allowed["monitored"] else 0
    db.update("movies", mid, allowed)
    return {"ok": True}


@endpoint
async def movie_delete(request: Request):
    library.delete_movie(_int(request, "id"), delete_files=request.query_params.get("delete_files") == "true")
    return {"ok": True}


@endpoint
async def movie_search(request: Request):
    mid = _int(request, "id")
    m = db.row("SELECT title FROM movies WHERE id = ?", mid)
    return search.jobs.enqueue(f"movie:{mid}", f"Searching {m['title']}", lambda: search.search_movie(mid))


@endpoint
async def movie_releases(request: Request):
    return await search.interactive_movie(_int(request, "id"))


@endpoint
async def movie_refresh(request: Request):
    mid = _int(request, "id")
    await library.refresh_movie(mid)
    return {"files_found": library.scan_movie(mid)}


@endpoint
async def movie_subtitles(request: Request):
    mid = _int(request, "id")
    m = db.row("SELECT file_id, title FROM movies WHERE id = ?", mid)
    if not m or not m["file_id"]:
        raise ApiError("No file yet")
    return {"result": await subs.process_file(m["file_id"], force=True)}


@endpoint
async def grab_release(request: Request):
    data = await body(request)
    did = await search.grab_token(data.get("token") or "")
    return {"download_id": did}


# ------------------------------------------------------------------- lookup --
@endpoint
async def lookup(request: Request):
    term = (request.query_params.get("term") or "").strip()
    kind = request.query_params.get("type") or "both"
    if not term:
        return {"results": [], "errors": {}}
    results: list[dict] = []
    errors: dict[str, str] = {}
    if kind in ("both", "tv"):
        try:
            results += await library.lookup_series(term)
        except Exception as e:
            errors["tv"] = f"Couldn't reach TVMaze: {e.__class__.__name__}"
    if kind in ("both", "movie"):
        try:
            results += await library.lookup_movies(term)
        except tmdb.TMDBKeyMissing as e:
            errors["movie"] = str(e)
        except Exception as e:
            errors["movie"] = f"Couldn't reach TMDB: {e.__class__.__name__}"
    t = term.lower()
    results.sort(key=lambda r: (r["title"].lower() != t, not r["title"].lower().startswith(t)))
    return {"results": results[:40], "errors": errors}


# --------------------------------------------------------------- activity ---
@endpoint
async def queue(request: Request):
    return {"items": downloads.queue()}


@endpoint
async def queue_remove(request: Request):
    did = _int(request, "id")
    d = db.row("SELECT * FROM downloads WHERE id = ?", did)
    if not d:
        raise ApiError("Not found", 404)
    blocklist = request.query_params.get("blocklist", "true") == "true"
    research = request.query_params.get("search", "true") == "true"
    await downloads.fail(d, "Removed by you", blocklist=blocklist, research=research)
    return {"ok": True}


@endpoint
async def queue_retry_import(request: Request):
    return {"result": await downloads.retry_import(_int(request, "id"))}


def _history(limit: int = 100, before: float | None = None, event: str | None = None) -> list[dict]:
    sql = "SELECT * FROM history WHERE 1=1"
    args: list = []
    if before:
        sql += " AND at < ?"
        args.append(before)
    if event:
        sql += " AND event = ?"
        args.append(event)
    sql += " ORDER BY at DESC LIMIT ?"
    args.append(limit)
    rows = db.rows(sql, *args)
    for r in rows:
        r["data"] = db.jload(r["data"], {})
        r["episode_ids"] = db.jload(r["episode_ids"], [])
    return rows


@endpoint
async def history(request: Request):
    before = request.query_params.get("before")
    return {"items": _history(int(request.query_params.get("limit", 100)), float(before) if before else None,
                              request.query_params.get("event") or None)}


@endpoint
async def blocklist(request: Request):
    return {"items": db.rows("SELECT * FROM blocklist ORDER BY at DESC LIMIT 500")}


@endpoint
async def blocklist_delete(request: Request):
    db.execute("DELETE FROM blocklist WHERE id = ?", _int(request, "id"))
    return {"ok": True}


@endpoint
async def logs(request: Request):
    return {"items": log.recent(int(request.query_params.get("limit", 300)), request.query_params.get("level"))}


def _calendar(days: int = 7, start: str | None = None) -> list[dict]:
    start_d = dt.date.fromisoformat(start) if start else dt.date.today()
    end_d = start_d + dt.timedelta(days=days)
    rows = db.rows("""SELECT e.*, s.title AS show, s.poster, s.monitored AS smon FROM episodes e
                      JOIN series s ON s.id = e.series_id
                      WHERE e.airdate >= ? AND e.airdate < ? AND e.season > 0 ORDER BY e.airdate, e.airstamp""",
                   start_d.isoformat(), end_d.isoformat())
    out = [{"kind": "episode", "date": r["airdate"], "title": r["show"], "series_id": r["series_id"],
            "detail": f"S{r['season']:02d}E{r['number']:02d}" + (f" · {r['title']}" if r["title"] else ""),
            "have": bool(r["file_id"]), "poster": r["poster"]} for r in rows]
    for m in db.rows("SELECT * FROM movies"):
        for key, label in (("digital_date", "Digital release"), ("physical_date", "Disc release"),
                           ("theatrical_date", "In cinemas")):
            d = m.get(key)
            if d and start_d.isoformat() <= d < end_d.isoformat():
                out.append({"kind": "movie", "date": d, "title": m["title"], "movie_id": m["id"],
                            "detail": f"Movie · {label}", "have": bool(m["file_id"]), "poster": m["poster"]})
    out.sort(key=lambda x: x["date"])
    return out


@endpoint
async def calendar(request: Request):
    return {"items": _calendar(int(request.query_params.get("days", 14)), request.query_params.get("start"))}


@endpoint
async def jobs(request: Request):
    return {"items": search.jobs.list(), "tasks": scheduler.status()}


@endpoint
async def run_task(request: Request):
    name = request.path_params["name"]
    if name == "search_missing":
        return {"result": await search.search_all_missing()}
    if name not in scheduler.TASKS:
        raise ApiError("Unknown task")
    return {"result": await scheduler.run(name)}


# ----------------------------------------------------------------- settings --
@endpoint
async def get_settings(request: Request):
    return {"settings": settings.public(), "profiles": quality.public_profiles(),
            "naming_tokens": naming.TOKENS}


@endpoint
async def put_settings(request: Request):
    section = request.path_params["section"]
    if section not in settings.DEFAULTS:
        raise ApiError("Unknown settings section")
    data = settings.strip_masks(section, await body(request))
    if section == "security":
        pw = data.pop("password", None)
        data.pop("password_hash", None)
        data.pop("secret", None)
        if pw:
            data["password_hash"] = hash_password(pw)
        if data.get("enabled") and not (data.get("password_hash") or settings.get("security").get("password_hash")):
            raise ApiError("Set a password before turning on the login.")
    if section == "folders":
        for key in ("tv", "movies"):
            if data.get(key):
                try:
                    os.makedirs(data[key], exist_ok=True)
                except OSError as e:
                    raise ApiError(f"Can't create {data[key]}: {e.strerror}")
    saved = settings.save(section, data)
    if section == "qbittorrent":
        qbittorrent.reset_client()
        _qbit_state["checked"] = 0
    return {"ok": True, "section": section, "values": settings.public()[section] if saved else {}}


@endpoint
async def test_qbit(request: Request):
    data = settings.strip_masks("qbittorrent", await body(request))
    cfg = {**settings.get("qbittorrent"), **data}
    client = qbittorrent.QbitClient(cfg)
    try:
        await client.login()
        version = await client.version()
        await client.ensure_category(cfg.get("category") or "omniarr")
        prefs = await client.preferences()
    finally:
        await client.close()
    save_path = prefs.get("save_path") or ""
    mapped = qbittorrent.map_path(save_path) if save_path else ""
    visible = bool(mapped) and os.path.isdir(mapped)
    settings.save("qbittorrent", {**data, "linked": True})
    qbittorrent.reset_client()
    _qbit_state["checked"] = 0
    hint = None
    if save_path and not visible:
        hint = (f"qBittorrent saves to {save_path}, which this app can't see. Add a folder mapping so finished "
                f"downloads can be imported.")
    return {"ok": True, "version": version, "save_path": save_path, "mapped": mapped, "visible": visible,
            "hint": hint}


@endpoint
async def test_flaresolverr(request: Request):
    data = await body(request)
    try:
        v = await net.flaresolverr_ping(data.get("url"))
        return {"ok": True, "message": f"Running · {v}"}
    except Exception as e:
        raise ApiError(f"FlareSolverr isn't reachable: {e}")


@endpoint
async def test_proxy(request: Request):
    data = await body(request)
    url = data.get("url") or settings.get("unblock").get("proxy_url")
    if not url:
        raise ApiError("Enter a proxy address first, e.g. http://warp:1080")
    try:
        return {"ok": True, "message": await net.test_proxy(url)}
    except net.IndexerError as e:
        raise ApiError(e.message)


@endpoint
async def test_tmdb(request: Request):
    data = await body(request)
    key = data.get("key")
    if not key or key == settings.MASK:
        key = settings.get("metadata").get("tmdb_key")
    try:
        await tmdb.test_key(key)
    except Exception as e:
        raise ApiError(f"TMDB didn't accept that key ({e.__class__.__name__})")
    return {"ok": True}


@endpoint
async def test_subtitle_provider(request: Request):
    data = await body(request)
    pid = data.get("id")
    current = {p["id"]: p for p in settings.get("subtitles")["providers"]}.get(pid, {})
    cfg = {**current, **{k: v for k, v in data.items() if v != settings.MASK}}
    try:
        msg = await subs.test_provider(pid, cfg)
    except Exception as e:
        raise ApiError(str(e) or e.__class__.__name__)
    return {"ok": True, "message": msg}


@endpoint
async def naming_preview(request: Request):
    data = await body(request)
    cfg = {**settings.get("naming"), **data}
    return naming.preview(cfg)


@endpoint
async def dismiss_setup(request: Request):
    settings.save("general", {"setup_dismissed": True})
    return {"ok": True}


# ----------------------------------------------------------------- indexers --
def _indexer_out(r: dict) -> dict:
    defn = None
    if r["kind"] == "cardigann":
        try:
            defn = catalog.get(r["definition_id"]).summary()
        except KeyError:
            defn = None
    cfg = db.jload(r["settings"], {}) or {}
    masked = {k: (settings.MASK if v and any(w in k.lower() for w in ("pass", "key", "cookie", "token", "pid"))
                  else v) for k, v in cfg.items()}
    return {"id": r["id"], "name": r["name"], "kind": r["kind"], "definition_id": r["definition_id"],
            "enabled": bool(r["enabled"]), "priority": r["priority"], "use_for": r["use_for"], "settings": masked,
            "flaresolverr": bool(r["flaresolverr"]), "use_proxy": bool(r["use_proxy"]),
            "seed_goals": bool(r["seed_goals"]), "query_limit": r["query_limit"], "grab_limit": r["grab_limit"],
            "queries_today": r["queries_today"] if r["day"] == dt.date.today().isoformat() else 0,
            "grabs_today": r["grabs_today"] if r["day"] == dt.date.today().isoformat() else 0,
            "health": manager.health(r), "type": (defn or {}).get("type") or ("torznab" if r["kind"] == "torznab"
                                                                             else "public"),
            "definition": defn}


@endpoint
async def indexers_list(request: Request):
    return {"items": [_indexer_out(r) for r in db.rows("SELECT * FROM indexers ORDER BY priority, name")]}


def _indexer_row_from(data: dict, existing: dict | None = None) -> dict:
    kind = data.get("kind") or (existing or {}).get("kind") or "cardigann"
    cfg_old = db.jload((existing or {}).get("settings"), {}) or {}
    cfg_new = {k: (cfg_old.get(k) if v == settings.MASK else v) for k, v in (data.get("settings") or {}).items()}
    name = data.get("name") or (existing or {}).get("name")
    if not name and kind == "cardigann":
        name = catalog.get(data["definition_id"]).name
    row = {
        "name": name or "Indexer", "kind": kind,
        "definition_id": data.get("definition_id") or (existing or {}).get("definition_id"),
        "enabled": 1 if data.get("enabled", True) else 0, "priority": int(data.get("priority") or 25),
        "use_for": data.get("use_for") or "both", "settings": db.jdump({**cfg_old, **cfg_new}),
        "flaresolverr": 1 if data.get("flaresolverr") else 0, "use_proxy": 1 if data.get("use_proxy") else 0,
        "seed_goals": 1 if data.get("seed_goals", True) else 0,
        "query_limit": int(data["query_limit"]) if data.get("query_limit") else None,
        "grab_limit": int(data["grab_limit"]) if data.get("grab_limit") else None,
    }
    return row


@endpoint
async def indexer_test_new(request: Request):
    """Test settings from the add/edit form before saving."""
    data = await body(request)
    existing = db.row("SELECT * FROM indexers WHERE id = ?", int(data["id"])) if data.get("id") else None
    row = _indexer_row_from(data, existing)
    row["id"] = existing["id"] if existing else 0
    row["cookies"] = existing["cookies"] if existing else "{}"
    row["user_agent"] = existing.get("user_agent") if existing else None
    res = await manager.test(row)
    if res.get("ok") is False and res.get("kind") == "cloudflare":
        res["message"] += " Make sure FlareSolverr is running (it's included in the docker-compose file)."
    res.pop("cookies", None)
    return res


@endpoint
async def indexer_create(request: Request):
    data = await body(request)
    if data.get("kind", "cardigann") == "cardigann":
        try:
            defn = catalog.get(data.get("definition_id") or "")
        except KeyError:
            raise ApiError("Unknown site")
        if defn.unsupported_reason() and "cookie" not in str(defn.unsupported_reason()):
            raise ApiError(defn.unsupported_reason())
    elif not (data.get("settings") or {}).get("url"):
        raise ApiError("Enter the Torznab address")
    row = _indexer_row_from(data)
    row["added_at"] = time.time()
    iid = db.insert("indexers", row)
    return _indexer_out(db.row("SELECT * FROM indexers WHERE id = ?", iid))


@endpoint
async def indexer_update(request: Request):
    iid = _int(request, "id")
    existing = db.row("SELECT * FROM indexers WHERE id = ?", iid)
    if not existing:
        raise ApiError("Not found", 404)
    data = await body(request)
    row = _indexer_row_from({**_indexer_out(existing), **data, "kind": existing["kind"]}, existing)
    row["disabled_until"] = None
    row["failures"] = 0
    row["last_error"] = None
    db.update("indexers", iid, row)
    manager.forget_session(iid)
    return _indexer_out(db.row("SELECT * FROM indexers WHERE id = ?", iid))


@endpoint
async def indexer_delete(request: Request):
    iid = _int(request, "id")
    db.execute("DELETE FROM indexers WHERE id = ?", iid)
    manager.forget_session(iid)
    return {"ok": True}


@endpoint
async def indexer_test(request: Request):
    iid = _int(request, "id")
    row = db.row("SELECT * FROM indexers WHERE id = ?", iid)
    if not row:
        raise ApiError("Not found", 404)
    res = await manager.test(row)
    res.pop("cookies", None)
    return res


@endpoint
async def indexers_add_recommended(request: Request):
    added = defaults.add_recommended()
    return {"added": added}


@endpoint
async def definitions(request: Request):
    q = (request.query_params.get("q") or "").lower().strip()
    t = request.query_params.get("type") or ""
    lang = request.query_params.get("lang") or ""
    items = list((await catalog.summaries()).values())
    have = {r["definition_id"] for r in db.rows("SELECT definition_id FROM indexers WHERE kind = 'cardigann'")}
    out = []
    for d in items:
        if q and q not in d["name"].lower() and q not in d["description"].lower() and q not in d["id"]:
            continue
        if t in ("public", "private", "semi-private") and d["type"] != t:
            continue
        if lang == "en" and not d["language"].lower().startswith("en"):
            continue
        out.append({k: v for k, v in d.items() if not k.startswith("_")} | {"added": d["id"] in have})
    out.sort(key=lambda d: (d["unsupported"] is not None, d["name"].lower()))
    return {"items": out[:300], "total": len(out)}


@endpoint
async def definition_detail(request: Request):
    try:
        d = catalog.get(request.path_params["id"])
    except KeyError:
        raise ApiError("Unknown site", 404)
    return {**d.summary(), "settings": d.settings_schema(),
            "request_delay": d.request_delay}


@endpoint
async def profiles(request: Request):
    return {"items": quality.public_profiles()}


routes = [
    Route("/api/auth/login", auth_login, methods=["POST"]),
    Route("/api/auth/logout", auth_logout, methods=["POST"]),
    Route("/api/auth/status", auth_status),
    Route("/api/status", status),
    Route("/api/dashboard", dashboard),
    Route("/api/library", library_list),
    Route("/api/lookup", lookup),
    Route("/api/series", series_add, methods=["POST"]),
    Route("/api/series/{id:int}", series_detail),
    Route("/api/series/{id:int}", series_update, methods=["PUT"]),
    Route("/api/series/{id:int}", series_delete, methods=["DELETE"]),
    Route("/api/series/{id:int}/refresh", series_refresh, methods=["POST"]),
    Route("/api/series/{id:int}/search", series_search, methods=["POST"]),
    Route("/api/series/{id:int}/subtitles", series_subtitles, methods=["POST"]),
    Route("/api/series/{id:int}/season-monitor", season_monitor, methods=["POST"]),
    Route("/api/series/{id:int}/releases", season_releases),
    Route("/api/episodes/{id:int}", episode_update, methods=["PUT"]),
    Route("/api/episodes/{id:int}/search", episode_search, methods=["POST"]),
    Route("/api/episodes/{id:int}/releases", episode_releases),
    Route("/api/files/{id:int}/subtitles", file_subtitles, methods=["POST"]),
    Route("/api/movies", movie_add, methods=["POST"]),
    Route("/api/movies/{id:int}", movie_detail),
    Route("/api/movies/{id:int}", movie_update, methods=["PUT"]),
    Route("/api/movies/{id:int}", movie_delete, methods=["DELETE"]),
    Route("/api/movies/{id:int}/search", movie_search, methods=["POST"]),
    Route("/api/movies/{id:int}/releases", movie_releases),
    Route("/api/movies/{id:int}/refresh", movie_refresh, methods=["POST"]),
    Route("/api/movies/{id:int}/subtitles", movie_subtitles, methods=["POST"]),
    Route("/api/grab", grab_release, methods=["POST"]),
    Route("/api/queue", queue),
    Route("/api/queue/{id:int}", queue_remove, methods=["DELETE"]),
    Route("/api/queue/{id:int}/import", queue_retry_import, methods=["POST"]),
    Route("/api/history", history),
    Route("/api/blocklist", blocklist),
    Route("/api/blocklist/{id:int}", blocklist_delete, methods=["DELETE"]),
    Route("/api/logs", logs),
    Route("/api/calendar", calendar),
    Route("/api/jobs", jobs),
    Route("/api/tasks/{name}", run_task, methods=["POST"]),
    Route("/api/settings", get_settings),
    Route("/api/settings/{section}", put_settings, methods=["PUT"]),
    Route("/api/test/qbittorrent", test_qbit, methods=["POST"]),
    Route("/api/test/flaresolverr", test_flaresolverr, methods=["POST"]),
    Route("/api/test/tmdb", test_tmdb, methods=["POST"]),
    Route("/api/test/proxy", test_proxy, methods=["POST"]),
    Route("/api/test/subtitles", test_subtitle_provider, methods=["POST"]),
    Route("/api/naming/preview", naming_preview, methods=["POST"]),
    Route("/api/setup/dismiss", dismiss_setup, methods=["POST"]),
    Route("/api/indexers", indexers_list),
    Route("/api/indexers", indexer_create, methods=["POST"]),
    Route("/api/indexers/test", indexer_test_new, methods=["POST"]),
    Route("/api/indexers/recommended", indexers_add_recommended, methods=["POST"]),
    Route("/api/indexers/{id:int}", indexer_update, methods=["PUT"]),
    Route("/api/indexers/{id:int}", indexer_delete, methods=["DELETE"]),
    Route("/api/indexers/{id:int}/test", indexer_test, methods=["POST"]),
    Route("/api/definitions", definitions),
    Route("/api/definitions/{id}", definition_detail),
    Route("/api/profiles", profiles),
]
