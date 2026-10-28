"""Runs searches across all indexers with caching, daily limits, back-off and health tracking."""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

from .. import db, log, settings
from ..net import IndexerError, Session
from . import catalog
from .base import Release
from .cardigann import CardigannIndexer
from .torznab import TorznabIndexer

L = log.get("indexers")

BACKOFF = [5 * 60, 15 * 60, 30 * 60, 3600, 3 * 3600, 6 * 3600]
_sessions: dict[int, Session] = {}
_cache: dict[tuple, tuple[float, list[Release]]] = {}


def _today() -> str:
    return dt.date.today().isoformat()


def _session(row: dict) -> Session:
    s = _sessions.get(row["id"])
    if s is None:
        s = Session(key=("indexer", row["id"]), name=row["name"])
        s.cookies = db.jload(row.get("cookies"), {}) or {}
        s.user_agent = row.get("user_agent") or None
        _sessions[row["id"]] = s
    s.name = row["name"]
    s.use_flaresolverr = bool(row.get("flaresolverr"))
    s.use_proxy = bool(row.get("use_proxy"))
    iid = row["id"]

    def _auto_fs(on: bool, iid=iid):
        db.update("indexers", iid, {"flaresolverr": 1 if on else 0})
        L.info("Turned on Cloudflare bypass for indexer %s (site started showing a browser check)", row["name"])
    s.on_flaresolverr_needed = _auto_fs
    return s


def forget_session(indexer_id: int) -> None:
    _sessions.pop(indexer_id, None)
    for k in [k for k in _cache if k[0] == indexer_id]:
        _cache.pop(k, None)


def instance(row: dict):
    cfg = db.jload(row.get("settings"), {}) or {}
    sess = _session(row)
    if row["kind"] == "torznab":
        return TorznabIndexer(row["id"], row["name"], cfg, sess, row.get("priority") or 25)
    defn = catalog.get(row["definition_id"])
    return CardigannIndexer(row["id"], row["name"], defn, cfg, sess, row.get("priority") or 25)


def _persist_session(row_id: int, sess: Session) -> None:
    db.update("indexers", row_id, {"cookies": db.jdump(sess.cookies), "user_agent": sess.user_agent})


def _roll_day(row: dict) -> dict:
    if row.get("day") != _today():
        db.update("indexers", row["id"], {"day": _today(), "queries_today": 0, "grabs_today": 0})
        row = {**row, "day": _today(), "queries_today": 0, "grabs_today": 0}
    return row


def availability(row: dict) -> str | None:
    """None if usable now, else a human reason."""
    if not row.get("enabled"):
        return "Turned off"
    now = time.time()
    if row.get("disabled_until") and row["disabled_until"] > now:
        mins = int((row["disabled_until"] - now) / 60) + 1
        return f"Paused {mins} min after errors — retrying"
    row = _roll_day(row)
    if settings.get("unblock").get("daily_limits"):
        if row.get("query_limit") and (row.get("queries_today") or 0) >= row["query_limit"]:
            return "Daily search limit reached"
    return None


def _record_ok(row: dict, ms: int, sess: Session) -> None:
    avg = row.get("avg_ms")
    avg = ms if not avg else int(avg * 0.7 + ms * 0.3)
    db.execute("UPDATE indexers SET failures = 0, disabled_until = NULL, last_error = NULL, last_error_kind = NULL, "
               "last_ok = ?, avg_ms = ?, queries_today = COALESCE(queries_today, 0) + 1, day = ? WHERE id = ?",
               time.time(), avg, _today(), row["id"])
    _persist_session(row["id"], sess)


def _record_fail(row: dict, err: IndexerError, sess: Session | None = None) -> None:
    fails = (db.scalar("SELECT failures FROM indexers WHERE id = ?", row["id"]) or 0) + 1
    until = None
    if settings.get("unblock").get("backoff"):
        until = time.time() + BACKOFF[min(fails - 1, len(BACKOFF) - 1)]
    db.execute("UPDATE indexers SET failures = ?, disabled_until = ?, last_error = ?, last_error_kind = ?, "
               "queries_today = COALESCE(queries_today, 0) + 1, day = ? WHERE id = ?",
               fails, until, err.message, err.kind, _today(), row["id"])
    if sess is not None:
        _persist_session(row["id"], sess)
    L.warning("Indexer %s failed (%s): %s", row["name"], err.kind, err.message)


def eligible_rows(kind: str | None, indexer_ids: list[int] | None = None) -> list[dict]:
    rows = db.rows("SELECT * FROM indexers ORDER BY priority, id")
    out = []
    for r in rows:
        if indexer_ids and r["id"] not in indexer_ids:
            continue
        if kind == "tv" and r["use_for"] == "movies":
            continue
        if kind == "movies" and r["use_for"] == "tv":
            continue
        out.append(r)
    return out


async def search(kind: str | None, keywords: str, *, title_only: str | None = None, season: int | None = None,
                 ep: int | None = None, imdb_id: str | None = None, tvdb_id: int | None = None,
                 tmdb_id: int | None = None, year: int | None = None, indexer_ids: list[int] | None = None,
                 use_cache: bool = True) -> tuple[list[Release], dict[str, str]]:
    """Search all eligible indexers in parallel (each one internally rate-limited)."""
    rows = eligible_rows(kind, indexer_ids)
    errors: dict[str, str] = {}
    cache_secs = float(settings.get("unblock").get("cache_minutes") or 0) * 60

    async def one(row: dict) -> list[Release]:
        reason = availability(row)
        if reason:
            errors[row["name"]] = reason
            return []
        key = (row["id"], kind, keywords, title_only, season, ep, imdb_id, tvdb_id, tmdb_id, year)
        hit = _cache.get(key)
        if use_cache and hit and time.time() - hit[0] < cache_secs:
            return hit[1]
        try:
            idx = instance(row)
        except KeyError as e:
            errors[row["name"]] = str(e)
            return []
        q = idx.build_query(kind or "search", keywords, season=season, ep=ep, imdb_id=imdb_id, tvdb_id=tvdb_id,
                            tmdb_id=tmdb_id, year=year)
        if isinstance(idx, TorznabIndexer):
            q["title_only"] = title_only or keywords
        t0 = time.time()
        try:
            rels = await idx.search(q)
        except IndexerError as e:
            _record_fail(row, e, idx.session)
            errors[row["name"]] = e.message
            return []
        except Exception as e:  # engine bug or unexpected site markup
            L.exception("Indexer %s crashed during search", row["name"])
            _record_fail(row, IndexerError("parse", f"Unexpected error: {e}"), idx.session)
            errors[row["name"]] = f"Unexpected error: {e}"
            return []
        _record_ok(row, int((time.time() - t0) * 1000), idx.session)
        if kind:
            rels = [r for r in rels if not r.kinds or kind in r.kinds or (kind == "tv" and "tv" in r.kinds)]
            rels = [r for r in rels if not (r.kinds and r.kinds == {"other"})]
        _cache[key] = (time.time(), rels)
        if len(_cache) > 2000:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:500]:
                _cache.pop(k, None)
        return rels

    results = await asyncio.gather(*(one(r) for r in rows))
    flat = [r for group in results for r in group]
    return flat, errors


async def rss(kind: str) -> list[Release]:
    rels, _ = await search(kind, "", use_cache=False)
    return rels


def grab_allowed(indexer_id: int) -> str | None:
    row = db.row("SELECT * FROM indexers WHERE id = ?", indexer_id)
    if not row:
        return "Indexer was removed"
    row = _roll_day(row)
    if settings.get("unblock").get("daily_limits") and row.get("grab_limit") and \
            (row.get("grabs_today") or 0) >= row["grab_limit"]:
        return "Daily download limit reached for this indexer"
    return None


async def fetch(rel: Release) -> tuple[bytes | None, str | None]:
    row = db.row("SELECT * FROM indexers WHERE id = ?", rel.indexer_id)
    if not row:
        if rel.magnet:
            return None, rel.magnet
        raise IndexerError("http", "The indexer for this release no longer exists.")
    idx = instance(row)
    try:
        data, magnet = await idx.fetch_torrent(rel)
    except IndexerError as e:
        _record_fail(row, e, idx.session)
        raise
    db.execute("UPDATE indexers SET grabs_today = COALESCE(grabs_today, 0) + 1, day = ? WHERE id = ?",
               _today(), row["id"])
    _persist_session(row["id"], idx.session)
    return data, magnet


async def test(row: dict) -> dict[str, Any]:
    forget_session(row.get("id") or 0)
    idx = instance(row)
    t0 = time.time()
    try:
        n = await idx.test()
    except IndexerError as e:
        if row.get("id"):
            _record_fail(row, e, idx.session)
        return {"ok": False, "kind": e.kind, "message": e.message,
                "flaresolverr": idx.session.use_flaresolverr}
    except Exception as e:
        L.exception("Indexer test crashed")
        return {"ok": False, "kind": "parse", "message": f"Unexpected error: {e}"}
    ms = int((time.time() - t0) * 1000)
    if row.get("id"):
        _record_ok(row, ms, idx.session)
    return {"ok": True, "results": n, "ms": ms, "flaresolverr": idx.session.use_flaresolverr,
            "cookies": idx.session.cookies, "user_agent": idx.session.user_agent}


def health(row: dict) -> dict:
    now = time.time()
    if not row.get("enabled"):
        return {"state": "off", "text": "Turned off"}
    if row.get("disabled_until") and row["disabled_until"] > now:
        mins = int((row["disabled_until"] - now) / 60) + 1
        return {"state": "paused", "text": f"Paused {mins} min after errors — retrying",
                "error": row.get("last_error"), "kind": row.get("last_error_kind")}
    if row.get("last_error"):
        return {"state": "error", "text": row["last_error"], "kind": row.get("last_error_kind")}
    if row.get("last_ok"):
        return {"state": "ok", "text": f"Healthy · {row.get('avg_ms') or 0} ms"}
    return {"state": "new", "text": "Not used yet"}
