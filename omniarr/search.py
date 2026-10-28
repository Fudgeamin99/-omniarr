"""Searching for wanted items, grabbing releases, RSS sync, and the background job queue."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Awaitable, Callable

from . import db, decisions as D, library, log, parser, qbittorrent, settings, torrent
from .indexers import manager
from .indexers.base import Release
from .net import IndexerError

L = log.get("search")


# ----------------------------------------------------------------- job queue --
class Jobs:
    def __init__(self):
        self.queue: asyncio.Queue | None = None
        self.items: dict[str, dict] = {}
        self.worker: asyncio.Task | None = None

    def start(self) -> None:
        if self.queue is None:
            self.queue = asyncio.Queue()
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())

    def enqueue(self, key: str, label: str, fn: Callable[[], Awaitable]) -> dict:
        self.start()
        for j in self.items.values():
            if j["key"] == key and j["state"] in ("queued", "running"):
                return j
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "key": key, "label": label, "state": "queued", "message": "", "added": time.time()}
        self.items[jid] = job
        self.queue.put_nowait((jid, fn))
        return job

    async def _run(self) -> None:
        while True:
            jid, fn = await self.queue.get()
            job = self.items.get(jid)
            if job is None:
                continue
            job["state"] = "running"
            job["started"] = time.time()
            try:
                res = await fn()
                job["state"] = "done"
                job["message"] = res if isinstance(res, str) else ""
            except Exception as e:
                L.exception("Job %s failed", job["label"])
                job["state"] = "failed"
                job["message"] = str(e)
            job["finished"] = time.time()
            # keep the list short
            done = [j for j in self.items.values() if j["state"] in ("done", "failed")]
            for old in sorted(done, key=lambda j: j.get("finished", 0))[:-30]:
                self.items.pop(old["id"], None)

    def list(self) -> list[dict]:
        return sorted(self.items.values(), key=lambda j: j["added"], reverse=True)

    async def drain(self, timeout: float = 120) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if not any(j["state"] in ("queued", "running") for j in self.items.values()):
                return
            await asyncio.sleep(0.2)


jobs = Jobs()

# releases shown in "pick a release myself", so the UI can grab one by token
_interactive: dict[str, tuple[float, D.Decision]] = {}


def _remember(decs: list[D.Decision]) -> list[dict]:
    now = time.time()
    for k in [k for k, (t, _) in _interactive.items() if now - t > 3600]:
        _interactive.pop(k, None)
    out = []
    for d in decs:
        tok = uuid.uuid4().hex
        _interactive[tok] = (now, d)
        item = d.to_dict()
        item["token"] = tok
        out.append(item)
    return out


def search_keywords(title: str) -> str:
    return parser.normalize_title(title).replace(" and ", " ")


# ------------------------------------------------------------------- grab ---
async def grab(d: D.Decision, *, manual: bool = False) -> int:
    rel = d.release
    reason = manager.grab_allowed(rel.indexer_id)
    if reason:
        raise IndexerError("limit", reason)
    data, magnet = await manager.fetch(rel)
    if data:
        h = torrent.info_hash(data)
    else:
        h = torrent.magnet_hash(magnet or "")
    if not h:
        raise IndexerError("parse", "Couldn't work out the torrent's hash.")
    qcfg = settings.get("qbittorrent")
    qb = qbittorrent.get_client()
    await qb.ensure_category(qcfg["category"])
    existing = db.row("SELECT id FROM downloads WHERE hash = ? AND status IN ('downloading','importing')", h)
    if existing:
        raise IndexerError("http", "That release is already downloading.")
    await qb.add(magnet=magnet if not data else None, torrent=data, category=qcfg["category"])
    row = db.row("SELECT * FROM indexers WHERE id = ?", rel.indexer_id) or {}
    if row.get("seed_goals", 1) and (rel.min_ratio or rel.min_seed_time):
        try:
            await qb.wait_for(h, timeout=5)
            await qb.set_share_limits(h, rel.min_ratio, int(rel.min_seed_time / 60) if rel.min_seed_time else None)
        except Exception as e:
            L.debug("Couldn't set share limits: %s", e)
    if d.kind == "episode":
        s = db.row("SELECT title FROM series WHERE id = ?", d.series_id)
        label = s["title"] if s else rel.title
    else:
        m = db.row("SELECT title, year FROM movies WHERE id = ?", d.movie_id)
        label = f"{m['title']} ({m['year']})" if m else rel.title
    did = db.insert("downloads", {
        "hash": h, "title": rel.title, "kind": d.kind, "series_id": d.series_id, "movie_id": d.movie_id,
        "episode_ids": db.jdump(d.episode_ids), "indexer_id": rel.indexer_id, "indexer_name": rel.indexer_name,
        "quality": d.parsed.quality, "size": rel.size, "release": db.jdump(rel.to_dict()), "status": "downloading",
        "progress": 0, "added_at": time.time(), "last_progress_at": time.time(),
    })
    seeds = f" · {rel.seeders} seeders" if rel.seeders is not None else ""
    db.add_history("grabbed", label, f"{rel.title} · {d.parsed.quality} from {rel.indexer_name}{seeds}",
                   kind=d.kind, series_id=d.series_id, movie_id=d.movie_id, episode_ids=d.episode_ids,
                   data={"release": rel.title, "indexer": rel.indexer_name, "manual": manual})
    L.info("Grabbed %s (%s) from %s", rel.title, d.parsed.quality, rel.indexer_name)
    return did


async def grab_token(token: str) -> int:
    hit = _interactive.get(token)
    if not hit:
        raise IndexerError("http", "That search result has expired — search again.")
    return await grab(hit[1], manual=True)


async def _grab_best(decs: list[D.Decision]) -> D.Decision | None:
    for d in D.best(decs):
        try:
            await grab(d)
            return d
        except (IndexerError, qbittorrent.QbitError) as e:
            L.warning("Couldn't grab %s: %s", d.release.title, e)
            if isinstance(e, qbittorrent.QbitError):
                raise
    return None


# --------------------------------------------------------------------- tv ---
def _series_ctx(sid: int):
    s = db.row("SELECT * FROM series WHERE id = ?", sid)
    eps = db.rows("SELECT * FROM episodes WHERE series_id = ? ORDER BY season, number", sid)
    return s, eps


async def _tv_releases(s: dict, *, season: int | None = None, ep: dict | None = None) -> tuple[list[Release], dict]:
    title = search_keywords(s["title"])
    kw = title
    if ep is not None:
        kw = f"{title} S{ep['season']:02d}E{ep['number']:02d}"
    elif season is not None:
        kw = f"{title} S{season:02d}"
    rels, errors = await manager.search("tv", kw, title_only=title, season=season if ep is None else ep["season"],
                                        ep=ep["number"] if ep else None, imdb_id=s.get("imdb_id"),
                                        tvdb_id=s.get("tvdb_id"))
    if ep is not None and s.get("is_anime") and ep.get("absolute"):
        more, e2 = await manager.search("tv", f"{title} {ep['absolute']:02d}", title_only=title)
        rels += more
        errors.update(e2)
    if ep is not None and not rels and ep.get("airdate"):
        more, e2 = await manager.search("tv", f"{title} {ep['airdate'].replace('-', ' ')}", title_only=title)
        rels += more
        errors.update(e2)
    if season is not None and ep is None and not rels:
        more, e2 = await manager.search("tv", f"{title} Season {season}", title_only=title)
        rels += more
        errors.update(e2)
    return rels, errors


def _evaluate_tv(rels: list[Release], s: dict, eps: list[dict]) -> list[D.Decision]:
    ctx = D.Context.load()
    names = D.series_names(s)
    seen, out = set(), []
    for r in rels:
        key = (r.title, r.indexer_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(D.evaluate_tv(r, s, eps, ctx, names=names))
    return out


async def interactive_episode(ep_id: int) -> dict:
    ep = db.row("SELECT * FROM episodes WHERE id = ?", ep_id)
    s, eps = _series_ctx(ep["series_id"])
    rels, errors = await _tv_releases(s, ep=ep)
    decs = _evaluate_tv(rels, s, eps)
    decs.sort(key=lambda d: (d.accepted, d.score()), reverse=True)
    return {"releases": _remember(decs), "errors": errors}


async def interactive_season(sid: int, season: int) -> dict:
    s, eps = _series_ctx(sid)
    rels, errors = await _tv_releases(s, season=season)
    decs = [d for d in _evaluate_tv(rels, s, eps)]
    decs.sort(key=lambda d: (d.accepted, d.score()), reverse=True)
    return {"releases": _remember(decs), "errors": errors}


async def search_episode(ep_id: int) -> str:
    ep = db.row("SELECT * FROM episodes WHERE id = ?", ep_id)
    if not ep:
        return "Episode not found"
    s, eps = _series_ctx(ep["series_id"])
    rels, errors = await _tv_releases(s, ep=ep)
    decs = _evaluate_tv(rels, s, eps)
    got = await _grab_best([d for d in decs if ep_id in d.episode_ids])
    if got:
        return f"Grabbed {got.release.title}"
    return _nothing_msg(rels, errors)


def _nothing_msg(rels, errors) -> str:
    if not rels and errors:
        return "No results — " + "; ".join(f"{k}: {v}" for k, v in list(errors.items())[:3])
    return f"Nothing suitable among {len(rels)} result(s)"


async def search_season(sid: int, season: int, *, fallback_episodes: bool = True) -> str:
    s, eps = _series_ctx(sid)
    season_eps = [e for e in eps if e["season"] == season]
    wanted = [e for e in season_eps if library.episode_wanted(e, s, _fq(e))]
    if not wanted:
        return "Nothing missing in this season"
    fully_aired = all(library.aired(e) for e in season_eps)
    msgs = []
    if fully_aired and len(wanted) >= max(2, len([e for e in season_eps if e["monitored"]]) // 2):
        rels, errors = await _tv_releases(s, season=season)
        decs = [d for d in _evaluate_tv(rels, s, eps) if d.season_pack]
        got = await _grab_best(decs)
        if got:
            return f"Grabbed season pack {got.release.title}"
        msgs.append(f"No suitable season pack ({len(rels)} results)")
    if fallback_episodes:
        grabbed = 0
        # re-read: a pack may have covered some
        for e in wanted:
            fresh_ctx = D.Context.load()
            if e["id"] in fresh_ctx.queued_episodes:
                continue
            res = await search_episode(e["id"])
            if res.startswith("Grabbed"):
                grabbed += 1
        msgs.append(f"Grabbed {grabbed} of {len(wanted)} episode(s)")
    return "; ".join(msgs)


def _fq(ep: dict) -> str | None:
    if not ep.get("file_id"):
        return None
    return db.scalar("SELECT quality FROM media_files WHERE id = ?", ep["file_id"])


async def search_series_missing(sid: int) -> str:
    s, eps = _series_ctx(sid)
    seasons = sorted({e["season"] for e in eps if e["season"] > 0 and library.episode_wanted(e, s, _fq(e))})
    if not seasons:
        return "Nothing missing"
    results = []
    for season in seasons:
        results.append(f"S{season:02d}: " + await search_season(sid, season))
    return " | ".join(results)


# ------------------------------------------------------------------ movies ---
async def _movie_releases(m: dict) -> tuple[list[Release], dict]:
    kw = search_keywords(m["title"])
    rels, errors = await manager.search("movies", f"{kw} {m['year']}" if m.get("year") else kw,
                                        imdb_id=m.get("imdb_id"), tmdb_id=m.get("tmdb_id"), year=m.get("year"))
    if not rels and m.get("original_title") and m["original_title"] != m["title"]:
        more, e2 = await manager.search("movies", f"{search_keywords(m['original_title'])} {m.get('year') or ''}".strip())
        rels += more
        errors.update(e2)
    return rels, errors


def _evaluate_movie(rels: list[Release], m: dict) -> list[D.Decision]:
    ctx = D.Context.load()
    seen, out = set(), []
    for r in rels:
        key = (r.title, r.indexer_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(D.evaluate_movie(r, m, ctx))
    return out


async def search_movie(mid: int) -> str:
    m = db.row("SELECT * FROM movies WHERE id = ?", mid)
    if not m:
        return "Movie not found"
    if not library.movie_available(m):
        return "Not released for home viewing yet — will grab automatically when it is"
    rels, errors = await _movie_releases(m)
    got = await _grab_best(_evaluate_movie(rels, m))
    if got:
        return f"Grabbed {got.release.title}"
    return _nothing_msg(rels, errors)


async def interactive_movie(mid: int) -> dict:
    m = db.row("SELECT * FROM movies WHERE id = ?", mid)
    rels, errors = await _movie_releases(m)
    decs = _evaluate_movie(rels, m)
    decs.sort(key=lambda d: (d.accepted, d.score()), reverse=True)
    return {"releases": _remember(decs), "errors": errors}


# --------------------------------------------------------------------- rss ---
async def rss_sync() -> str:
    if not settings.get("search").get("rss_enabled", True):
        return "RSS turned off"
    series = db.rows("SELECT * FROM series WHERE monitored = 1")
    movies = db.rows("SELECT * FROM movies WHERE monitored = 1")
    if not series and not movies:
        return "Library is empty"
    rels: list[Release] = []
    if series:
        rels += await manager.rss("tv")
    if movies:
        rels += await manager.rss("movies")
    s_names = [(s, D.series_names(s)) for s in series]
    ctx = D.Context.load()
    eps_cache: dict[int, list] = {}
    tv_decs: list[D.Decision] = []
    mv_decs: list[D.Decision] = []
    for r in rels:
        p = parser.parse(r.title)
        if p.is_tv:
            for s, names in s_names:
                if D.title_matches_series(p, s, names):
                    eps = eps_cache.setdefault(s["id"], db.rows("SELECT * FROM episodes WHERE series_id = ?", s["id"]))
                    tv_decs.append(D.evaluate_tv(r, s, eps, ctx, parsed=p, names=names, require_title=False))
                    break
        else:
            for m in movies:
                if D.title_matches_movie(p, m):
                    mv_decs.append(D.evaluate_movie(r, m, ctx, parsed=p, require_title=False))
                    break
    grabbed = 0
    taken_eps: set = set()
    for d in D.best(tv_decs):
        if taken_eps & set(d.episode_ids):
            continue
        try:
            await grab(d)
            taken_eps |= set(d.episode_ids)
            grabbed += 1
        except Exception as e:
            L.warning("RSS grab failed for %s: %s", d.release.title, e)
    taken_movies: set = set()
    for d in D.best(mv_decs):
        if d.movie_id in taken_movies:
            continue
        try:
            await grab(d)
            taken_movies.add(d.movie_id)
            grabbed += 1
        except Exception as e:
            L.warning("RSS grab failed for %s: %s", d.release.title, e)
    return f"Checked {len(rels)} new releases, grabbed {grabbed}"


async def search_all_missing() -> str:
    count = 0
    for s in db.rows("SELECT id, title FROM series WHERE monitored = 1"):
        jobs.enqueue(f"series:{s['id']}", f"Searching {s['title']}", lambda sid=s["id"]: search_series_missing(sid))
        count += 1
    for m in db.rows("SELECT * FROM movies WHERE monitored = 1 AND file_id IS NULL"):
        if library.movie_available(m):
            jobs.enqueue(f"movie:{m['id']}", f"Searching {m['title']}", lambda mid=m["id"]: search_movie(mid))
            count += 1
    return f"Queued {count} searches"
