"""Watches qBittorrent, imports finished downloads into the library, handles failures and seeding clean-up."""
from __future__ import annotations

import asyncio
import errno
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import db, library, log, naming, parser, qbittorrent, quality, settings

L = log.get("downloads")

ARCHIVE_EXTS = {".rar", ".zip", ".7z"}
_subtitle_hook = None   # set by subtitles module to avoid import cycle


def set_subtitle_hook(fn) -> None:
    global _subtitle_hook
    _subtitle_hook = fn


class ImportProblem(Exception):
    pass


# ---------------------------------------------------------------- monitor ---
async def check() -> str:
    active = db.rows("SELECT * FROM downloads WHERE status IN ('downloading', 'importing')")
    seeding = db.rows("SELECT * FROM downloads WHERE status = 'imported' AND removed_from_client = 0")
    if not active and not seeding:
        return "Nothing downloading"
    qcfg = settings.get("qbittorrent")
    qb = qbittorrent.get_client()
    try:
        torrents = {t["hash"].lower(): t for t in await qb.torrents(category=qcfg["category"])}
    except qbittorrent.QbitError as e:
        L.warning("Couldn't check qBittorrent: %s", e)
        return str(e)
    stall_secs = float(settings.get("search").get("stall_hours") or 6) * 3600
    now = time.time()
    imported = 0
    for d in active:
        t = torrents.get((d["hash"] or "").lower())
        if t is None:
            if now - (d["added_at"] or now) < 120:
                continue   # qBittorrent may still be fetching magnet metadata
            db.update("downloads", d["id"], {"status": "removed", "message": "Removed from qBittorrent",
                                             "finished_at": now})
            db.add_history("removed", d["title"], "Removed from qBittorrent before it finished", kind=d["kind"],
                           series_id=d["series_id"], movie_id=d["movie_id"],
                           episode_ids=db.jload(d["episode_ids"], []))
            continue
        progress = float(t.get("progress") or 0)
        upd = {"progress": progress, "state": t.get("state"), "eta": t.get("eta"), "dlspeed": t.get("dlspeed")}
        if progress > (d["last_progress"] or 0) + 0.0005:
            upd["last_progress"] = progress
            upd["last_progress_at"] = now
        db.update("downloads", d["id"], upd)
        state = t.get("state") or ""
        if state in qbittorrent.ERROR_STATES:
            await fail(d, f"qBittorrent reported '{state}'")
            continue
        if progress >= 1.0 and state in qbittorrent.COMPLETED_STATES:
            try:
                await import_download({**d, **upd}, t)
                imported += 1
            except ImportProblem as e:
                db.update("downloads", d["id"], {"status": "import_blocked", "message": str(e)})
                L.warning("Import needs attention for %s: %s", d["title"], e)
            except Exception as e:
                L.exception("Import crashed for %s", d["title"])
                db.update("downloads", d["id"], {"status": "import_blocked", "message": f"Unexpected error: {e}"})
            continue
        last = d["last_progress_at"] or d["added_at"] or now
        if "last_progress_at" in upd:
            last = now
        if state in qbittorrent.STALL_STATES and now - last > stall_secs:
            hrs = int(stall_secs // 3600)
            await fail(d, f"Stalled with no progress for {hrs} h")
        elif state not in qbittorrent.COMPLETED_STATES and now - last > stall_secs * 4:
            await fail(d, "No progress for a long time")
    await _cleanup_seeding(seeding, torrents)
    return f"{len(active)} active, {imported} imported"


async def fail(d: dict, reason: str, *, blocklist: bool = True, research: bool | None = None,
               remove_from_client: bool = True) -> None:
    now = time.time()
    db.update("downloads", d["id"], {"status": "failed", "message": reason, "finished_at": now})
    rel = db.jload(d.get("release"), {}) or {}
    if blocklist:
        db.insert("blocklist", {"at": now, "title": d["title"], "hash": d["hash"], "guid": rel.get("guid"),
                                "indexer_name": d["indexer_name"], "series_id": d["series_id"],
                                "movie_id": d["movie_id"], "reason": reason})
    if research is None:
        research = bool(settings.get("search")["auto_research_failed"])
    db.add_history("failed", d["title"], reason + (" — blocked" if blocklist else "") +
                   (" and searching again" if research else ""), kind=d["kind"], series_id=d["series_id"], movie_id=d["movie_id"],
                   episode_ids=db.jload(d["episode_ids"], []))
    L.info("Download failed: %s (%s)", d["title"], reason)
    if remove_from_client and d.get("hash"):
        try:
            await qbittorrent.get_client().delete(d["hash"], delete_files=True)
        except Exception as e:
            L.debug("Couldn't remove failed torrent: %s", e)
    if research:
        from . import search
        if d["kind"] == "movie" and d["movie_id"]:
            search.jobs.enqueue(f"movie:{d['movie_id']}", f"Re-searching after failure",
                                lambda mid=d["movie_id"]: search.search_movie(mid))
        else:
            for eid in db.jload(d["episode_ids"], []):
                search.jobs.enqueue(f"episode:{eid}", "Re-searching after failure",
                                    lambda e=eid: search.search_episode(e))


async def _cleanup_seeding(rows: list[dict], torrents: dict) -> None:
    if not settings.get("qbittorrent").get("remove_completed"):
        return
    qb = qbittorrent.get_client()
    for d in rows:
        t = torrents.get((d["hash"] or "").lower())
        if t is None:
            db.update("downloads", d["id"], {"removed_from_client": 1})
            continue
        rel = db.jload(d["release"], {}) or {}
        ratio_goal = rel.get("min_ratio")
        time_goal = rel.get("min_seed_time")
        state = t.get("state") or ""
        done_by_qbit = state in ("pausedUP", "stoppedUP")
        goals_set = ratio_goal is not None or time_goal is not None
        goals_met = goals_set and (ratio_goal is None or (t.get("ratio") or 0) >= ratio_goal) and \
            (time_goal is None or (t.get("seeding_time") or 0) >= time_goal)
        if done_by_qbit or goals_met:
            try:
                # the library has its own hardlink/copy, so the download copy can go
                await qb.delete(d["hash"], delete_files=True)
                db.update("downloads", d["id"], {"removed_from_client": 1})
                L.info("Finished seeding %s — removed from qBittorrent", d["title"])
            except Exception as e:
                L.debug("Couldn't remove seeded torrent: %s", e)


# ----------------------------------------------------------------- import ---
def _content_root(t: dict) -> Path:
    content = t.get("content_path") or ""
    if not content:
        content = os.path.join(t.get("save_path") or "", t.get("name") or "")
    return Path(qbittorrent.map_path(content))


def _find_videos(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in parser.VIDEO_EXTS else []
    vids = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            if p.suffix.lower() not in parser.VIDEO_EXTS:
                continue
            low = p.name.lower()
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if ("sample" in low and size < 400_000_000) or "/extras/" in str(p).lower() or \
                    "/featurettes/" in str(p).lower() or low.endswith(".iso"):
                continue
            vids.append(p)
    return vids


def _first_volume(p: Path) -> bool:
    n = p.name.lower()
    m = re.search(r"\.part(\d+)\.rar$", n)
    if m:
        return int(m.group(1)) == 1
    return p.suffix.lower() in ARCHIVE_EXTS


def _extract_archives(root: Path) -> Path | None:
    if root.is_file():
        return None
    archives = [p for p in root.rglob("*") if _first_volume(p)]
    if not archives:
        return None
    tool = shutil.which("unar") or shutil.which("7z")
    if not tool:
        raise ImportProblem("The download is packed in archives (RAR) and no extractor is installed.")
    out = root / "_omniarr_unpacked"
    out.mkdir(exist_ok=True)
    for a in archives:
        cmd = [tool, "-q", "-f", "-o", str(out), str(a)] if tool.endswith("unar") else \
            [tool, "x", "-y", f"-o{out}", str(a)]
        try:
            subprocess.run(cmd, check=True, timeout=3600, capture_output=True)
        except (subprocess.SubprocessError, OSError) as e:
            raise ImportProblem(f"Couldn't unpack {a.name}: {e}") from e
    return out


def _place(src: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".omniarr-partial")
    if tmp.exists():
        tmp.unlink()
    method = "copy"
    if settings.get("folders").get("use_hardlinks", True):
        try:
            os.link(src, tmp)
            method = "hardlink"
        except OSError as e:
            if e.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EACCES):
                raise
    if method == "copy":
        shutil.copy2(src, tmp)
    os.replace(tmp, dest)
    return method


def _remove_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    # sidecar subtitles of the old file
    base = os.path.splitext(path)[0]
    folder = os.path.dirname(path)
    if os.path.isdir(folder):
        for f in os.listdir(folder):
            full = os.path.join(folder, f)
            if full.startswith(base + ".") and os.path.splitext(f)[1].lower() in parser.SUB_EXTS:
                try:
                    os.remove(full)
                except OSError:
                    pass


async def import_download(d: dict, t: dict) -> None:
    db.update("downloads", d["id"], {"status": "importing"})
    root = _content_root(t)
    if not root.exists():
        raise ImportProblem(f"Can't see the downloaded files at {root}. If qBittorrent runs elsewhere, "
                            f"add a folder mapping in Connections → qBittorrent.")
    vids = await asyncio.to_thread(_find_videos, root)
    if not vids:
        unpacked = await asyncio.to_thread(_extract_archives, root)
        if unpacked:
            vids = await asyncio.to_thread(_find_videos, unpacked)
    if not vids:
        raise ImportProblem("No video files found in the download.")
    rel_parsed = parser.parse(d["title"])
    if d["kind"] == "movie":
        files = await _import_movie(d, vids, rel_parsed)
    else:
        files = await _import_episodes(d, vids, rel_parsed)
    db.update("downloads", d["id"], {"status": "imported", "finished_at": time.time(),
                                     "message": f"Imported {len(files)} file(s)"})
    for fid in files:
        if _subtitle_hook:
            _subtitle_hook(fid)


def _file_quality(vid: Path, rel_parsed: parser.Parsed) -> tuple[str, int, str | None]:
    fp = parser.parse(vid.name)
    q = fp.quality if fp.quality != "Unknown" else rel_parsed.quality
    if rel_parsed.quality != "Unknown" and quality.tier(rel_parsed.quality) > quality.tier(q) and \
            fp.resolution in (None, rel_parsed.resolution):
        q = rel_parsed.quality
    return q, max(fp.revision, rel_parsed.revision), fp.group or rel_parsed.group


async def _import_episodes(d: dict, vids: list[Path], rel_parsed: parser.Parsed) -> list[int]:
    s = db.row("SELECT * FROM series WHERE id = ?", d["series_id"])
    if not s:
        raise ImportProblem("The show was removed from the library.")
    eps = db.rows("SELECT * FROM episodes WHERE series_id = ?", s["id"])
    by_key = {(e["season"], e["number"]): e for e in eps}
    by_abs = {e["absolute"]: e for e in eps if e.get("absolute")}
    by_date = {e["airdate"]: e for e in eps if e.get("airdate")}
    wanted_ids = set(db.jload(d["episode_ids"], []))
    name_cfg = settings.get("naming")
    rename = name_cfg.get("rename", True)
    imported: list[int] = []
    plan: list[tuple[Path, list[dict]]] = []
    for v in vids:
        p = parser.parse(v.name)
        if not (p.episodes or p.absolute or p.air_date):
            # file named "01.mkv" inside "Show.S01" folder: borrow season from the release/folder
            p2 = parser.parse(f"{v.parent.name} {v.name}")
            if p2.episodes:
                p = p2
            elif rel_parsed.episodes and len(vids) == 1:
                p = rel_parsed
        targets: list[dict] = []
        if p.seasons and p.episodes:
            season = p.seasons[0]
            targets = [by_key[(season, n)] for n in p.episodes if (season, n) in by_key]
        elif p.absolute:
            targets = [by_abs[n] for n in p.absolute if n in by_abs]
        elif p.air_date:
            targets = [by_date[p.air_date]] if p.air_date in by_date else []
        if not targets and len(vids) == 1 and len(wanted_ids) >= 1:
            targets = [e for e in eps if e["id"] in wanted_ids]
        if not targets:
            L.info("Skipping %s — couldn't match it to an episode", v.name)
            continue
        plan.append((v, targets))
    if not plan:
        raise ImportProblem("Couldn't match any file in the download to an episode of " + s["title"] + ".")
    for v, targets in plan:
        q, rev, group = _file_quality(v, rel_parsed)
        season = targets[0]["season"]
        ext = v.suffix.lower()
        if rename:
            folder = Path(s["path"]) / naming.season_folder(name_cfg["season_folder"], season)
            fname = naming.episode_file(name_cfg["episode"], s["title"], s["year"], season,
                                        [{"number": t["number"], "title": t["title"], "airdate": t["airdate"]}
                                         for t in targets], q, group) + ext
        else:
            folder = Path(s["path"]) / naming.season_folder(name_cfg["season_folder"], season)
            fname = v.name
        dest = folder / fname
        # upgrades: drop the old file(s) for these episodes
        old_ids = {t["file_id"] for t in targets if t.get("file_id")}
        for oid in old_ids:
            old = db.row("SELECT * FROM media_files WHERE id = ?", oid)
            if old and old["path"] != str(dest):
                await asyncio.to_thread(_remove_file, old["path"])
            db.execute("UPDATE episodes SET file_id = NULL WHERE file_id = ?", oid)
            db.execute("DELETE FROM media_files WHERE id = ?", oid)
        method = await asyncio.to_thread(_place, v, dest)
        db.execute("DELETE FROM media_files WHERE path = ?", str(dest))
        fid = db.insert("media_files", {
            "kind": "episode", "series_id": s["id"], "path": str(dest), "size": dest.stat().st_size, "quality": q,
            "revision": rev, "release_group": group, "release_title": d["title"],
            "languages": db.jdump(sorted(rel_parsed.languages)), "added_at": time.time(), "sub_status": "pending"})
        for t in targets:
            db.update("episodes", t["id"], {"file_id": fid})
        imported.append(fid)
        label = f"{s['title']} S{season:02d}E" + "-E".join(f"{t['number']:02d}" for t in targets)
        db.add_history("imported", label, f"{'Hardlinked' if method == 'hardlink' else 'Copied'} to {dest}",
                       kind="episode", series_id=s["id"], episode_ids=[t["id"] for t in targets],
                       data={"path": str(dest), "quality": q, "method": method})
        L.info("Imported %s → %s (%s)", v.name, dest, method)
    return imported


async def _import_movie(d: dict, vids: list[Path], rel_parsed: parser.Parsed) -> list[int]:
    m = db.row("SELECT * FROM movies WHERE id = ?", d["movie_id"])
    if not m:
        raise ImportProblem("The movie was removed from the library.")
    v = max(vids, key=lambda p: p.stat().st_size)
    q, rev, group = _file_quality(v, rel_parsed)
    name_cfg = settings.get("naming")
    folder = Path(m["path"])
    fname = (naming.movie_file(name_cfg["movie_file"], m["title"], m["year"], q, group) + v.suffix.lower()) \
        if name_cfg.get("rename", True) else v.name
    dest = folder / fname
    if m.get("file_id"):
        old = db.row("SELECT * FROM media_files WHERE id = ?", m["file_id"])
        if old and old["path"] != str(dest):
            await asyncio.to_thread(_remove_file, old["path"])
        db.update("movies", m["id"], {"file_id": None})
        db.execute("DELETE FROM media_files WHERE id = ?", m["file_id"])
    method = await asyncio.to_thread(_place, v, dest)
    db.execute("DELETE FROM media_files WHERE path = ?", str(dest))
    fid = db.insert("media_files", {
        "kind": "movie", "movie_id": m["id"], "path": str(dest), "size": dest.stat().st_size, "quality": q,
        "revision": rev, "release_group": group, "release_title": d["title"],
        "languages": db.jdump(sorted(rel_parsed.languages)), "added_at": time.time(), "sub_status": "pending"})
    db.update("movies", m["id"], {"file_id": fid})
    db.add_history("imported", f"{m['title']} ({m['year']})",
                   f"{'Hardlinked' if method == 'hardlink' else 'Copied'} to {dest}", kind="movie", movie_id=m["id"],
                   data={"path": str(dest), "quality": q, "method": method})
    L.info("Imported %s → %s (%s)", v.name, dest, method)
    return [fid]


async def retry_import(did: int) -> str:
    d = db.row("SELECT * FROM downloads WHERE id = ?", did)
    if not d:
        return "Not found"
    qb = qbittorrent.get_client()
    items = await qb.torrents(hashes=[d["hash"]])
    if not items:
        return "The torrent is no longer in qBittorrent"
    try:
        await import_download(d, items[0])
        return "Imported"
    except ImportProblem as e:
        db.update("downloads", d["id"], {"status": "import_blocked", "message": str(e)})
        return str(e)


def queue() -> list[dict]:
    rows = db.rows("SELECT * FROM downloads WHERE status IN ('downloading','importing','import_blocked') "
                   "ORDER BY added_at DESC")
    out = []
    for r in rows:
        eps = db.jload(r["episode_ids"], [])
        label = r["title"]
        if r["kind"] == "episode" and r["series_id"]:
            s = db.row("SELECT title FROM series WHERE id = ?", r["series_id"])
            ep_rows = db.rows(f"SELECT season, number FROM episodes WHERE id IN ({','.join('?' * len(eps))})",
                              *eps) if eps else []
            if s:
                if len(ep_rows) == 1:
                    label = f"{s['title']} · S{ep_rows[0]['season']:02d}E{ep_rows[0]['number']:02d}"
                elif ep_rows:
                    seasons = sorted({e["season"] for e in ep_rows})
                    label = f"{s['title']} · " + (f"Season {seasons[0]}" if len(seasons) == 1 else
                                                  f"Seasons {seasons[0]}–{seasons[-1]}") + f" ({len(ep_rows)} eps)"
                else:
                    label = s["title"]
        elif r["kind"] == "movie" and r["movie_id"]:
            m = db.row("SELECT title, year FROM movies WHERE id = ?", r["movie_id"])
            if m:
                label = f"{m['title']} ({m['year']})"
        out.append({**{k: r[k] for k in ("id", "hash", "title", "kind", "series_id", "movie_id", "indexer_name",
                                         "quality", "size", "status", "progress", "state", "eta", "dlspeed",
                                         "message", "added_at")}, "label": label})
    return out
