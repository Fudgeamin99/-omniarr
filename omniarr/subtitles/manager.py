"""Find, score and save subtitles next to imported videos (built-in Bazarr)."""
from __future__ import annotations

import asyncio
import os
import re
import time

from charset_normalizer import from_bytes

from .. import db, downloads, log, mediainfo, parser, settings
from .providers import Candidate, ProviderError, Query, build

L = log.get("subtitles")
_pending: set[int] = set()


def _tokens(name: str) -> dict:
    p = parser.parse(name)
    return {"group": (p.group or "").lower(), "source": p.source, "resolution": p.resolution,
            "codec": (p.codec or "").replace("h", "x") if p.codec else None, "streaming": p.streaming,
            "revision": p.revision}


def score(c: Candidate, video_name: str, mode: str) -> int:
    v = _tokens(video_name)
    best = 0
    for rel in c.releases or [""]:
        r = _tokens(rel)
        s = 0
        if v["group"] and r["group"] and v["group"] == r["group"]:
            s += 40
        elif v["group"] and v["group"] in rel.lower():
            s += 30
        if v["source"] and v["source"] == r["source"]:
            s += 20
        if v["resolution"] and v["resolution"] == r["resolution"]:
            s += 10
        if v["streaming"] and v["streaming"] == r["streaming"]:
            s += 10
        if v["codec"] and v["codec"] == r["codec"]:
            s += 5
        best = max(best, s)
    if c.hash_match:
        best += 100
    if mode == "hi":
        best += 8 if c.hearing_impaired else 0
    elif mode in ("normal", "normal_hi_fallback"):
        best -= 8 if c.hearing_impaired else 0
    best += min(5, c.downloads // 2000)
    return best


def _existing_external(video: str, lang: str) -> str | None:
    base, _ = os.path.splitext(video)
    folder = os.path.dirname(video)
    if not os.path.isdir(folder):
        return None
    stem = os.path.basename(base)
    for f in os.listdir(folder):
        if not f.startswith(stem + "."):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext not in parser.SUB_EXTS:
            continue
        middle = f[len(stem) + 1:-len(ext)].lower().split(".")
        if lang in middle or {"en": "eng"}.get(lang) in middle or {"en": "english"}.get(lang) in middle:
            return os.path.join(folder, f)
    return None


def _to_utf8(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        best = from_bytes(data).best()
        return str(best) if best else data.decode("latin-1", "replace")


def _looks_like_subtitle(text: str, ext: str) -> bool:
    if ext == ".srt":
        return "-->" in text[:5000]
    if ext in (".ass", ".ssa"):
        return "[Script Info]" in text[:2000] or "Dialogue:" in text
    return len(text) > 50


def _query_for(f: dict) -> Query | None:
    if f["kind"] == "movie":
        m = db.row("SELECT * FROM movies WHERE id = ?", f["movie_id"])
        if not m:
            return None
        return Query("movie", m["title"], m["year"], None, None, m.get("imdb_id"), m.get("tmdb_id"),
                     mediainfo.opensubtitles_hash(f["path"]), f.get("release_title") or os.path.basename(f["path"]))
    eps = db.rows("SELECT * FROM episodes WHERE file_id = ? ORDER BY season, number", f["id"])
    s = db.row("SELECT * FROM series WHERE id = ?", f["series_id"])
    if not eps or not s:
        return None
    return Query("episode", s["title"], s["year"], eps[0]["season"], eps[0]["number"], s.get("imdb_id"), None,
                 mediainfo.opensubtitles_hash(f["path"]), f.get("release_title") or os.path.basename(f["path"]))


async def process_file(file_id: int, *, force: bool = False) -> str:
    cfg = settings.get("subtitles")
    f = db.row("SELECT * FROM media_files WHERE id = ?", file_id)
    if not f:
        return "File not found"
    if not cfg.get("enabled"):
        db.update("media_files", file_id, {"sub_status": "disabled"})
        return "Subtitles turned off"
    if not os.path.exists(f["path"]):
        return "Video file is missing"
    have = db.jload(f["subtitles"], {}) or {}
    langs = cfg.get("languages") or ["en"]
    mode = cfg.get("mode") or "normal_hi_fallback"
    results = []
    status = "ok"
    for lang in langs:
        if lang in have and not force and not (cfg.get("upgrade") and (have[lang].get("score") or 0) < 60):
            if os.path.exists(have[lang].get("path") or "") or have[lang].get("embedded"):
                continue
        ext_path = _existing_external(f["path"], lang)
        if ext_path and not force and lang not in have:
            have[lang] = {"path": ext_path, "provider": "existing", "score": 100}
            continue
        if cfg.get("skip_embedded", True) and mode != "forced":
            try:
                emb = await asyncio.to_thread(mediainfo.embedded_languages, f["path"])
            except Exception:
                emb = set()
            if lang in emb:
                have[lang] = {"embedded": True, "provider": "embedded", "score": 100}
                status = "embedded" if status == "ok" else status
                continue
        q = _query_for(f)
        if not q:
            continue
        q.language = lang
        got = await _search_and_save(f, q, lang, mode)
        if got:
            have[lang] = got
            results.append(f"{lang}: {got['provider']} ({got['score']})")
        else:
            status = "missing"
    if status == "ok" and all(v.get("embedded") for v in have.values()) and have:
        status = "embedded"
    db.update("media_files", file_id, {"subtitles": db.jdump(have), "sub_status": status,
                                       "sub_checked_at": time.time(),
                                       "sub_score": min((v.get("score") or 0) for v in have.values()) if have else None})
    return "; ".join(results) or status


async def _search_and_save(f: dict, q: Query, lang: str, mode: str) -> dict | None:
    providers = build(settings.get("subtitles").get("providers") or [])
    cands: list[Candidate] = []
    for p in providers:
        try:
            found = await p.search(q)
        except (ProviderError, Exception) as e:
            L.warning("%s search failed for %s: %s", p.name, os.path.basename(f["path"]), e)
            continue
        for c in found:
            c.extra["provider_obj"] = p
        cands.extend(found)
    if mode == "forced":
        cands = [c for c in cands if c.forced]
    else:
        cands = [c for c in cands if not c.forced]
        if mode == "normal":
            cands = [c for c in cands if not c.hearing_impaired]
    video_name = q.release_name or os.path.basename(f["path"])
    for c in cands:
        c.score = score(c, video_name, mode)
    cands.sort(key=lambda c: c.score, reverse=True)
    for c in cands[:4]:
        prov = c.extra["provider_obj"]
        try:
            data, ext = await prov.download(c)
        except Exception as e:
            L.warning("%s download failed: %s", prov.name, e)
            continue
        text = _to_utf8(data)
        if not _looks_like_subtitle(text, ext):
            continue
        base = os.path.splitext(f["path"])[0]
        suffix = f".{lang}" + (".hi" if c.hearing_impaired else "") + (".forced" if c.forced else "")
        path = base + suffix + ext
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        label = os.path.basename(f["path"])
        db.add_history("subtitle", label, f"{_lang_name(lang)} subtitle saved ({prov.name}, "
                                          f"{'exact file match' if c.hash_match else f'match score {c.score}'})",
                       kind=f["kind"], series_id=f["series_id"], movie_id=f["movie_id"],
                       data={"path": path, "provider": prov.id, "score": c.score})
        L.info("Saved %s subtitle for %s from %s (score %d)", lang, label, prov.name, c.score)
        return {"path": path, "provider": prov.id, "score": c.score, "hi": c.hearing_impaired}
    return None


def _lang_name(code: str) -> str:
    return {"en": "English"}.get(code, code)


def queue_file(file_id: int) -> None:
    """Called after import; runs in the background."""
    if file_id in _pending:
        return
    _pending.add(file_id)

    async def run():
        try:
            await process_file(file_id)
        except Exception:
            L.exception("Subtitle search crashed")
        finally:
            _pending.discard(file_id)
    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        _pending.discard(file_id)


downloads.set_subtitle_hook(queue_file)


async def sweep() -> str:
    """Retry files still missing subtitles (newer files more often)."""
    cfg = settings.get("subtitles")
    if not cfg.get("enabled"):
        return "Subtitles turned off"
    now = time.time()
    rows = db.rows("SELECT * FROM media_files WHERE sub_status IN ('pending', 'missing') "
                   + ("" if cfg.get("retry", True) else "AND sub_status = 'pending' ") +
                   "ORDER BY added_at DESC LIMIT 200")
    if cfg.get("upgrade"):
        rows += db.rows("SELECT * FROM media_files WHERE sub_status = 'ok' AND COALESCE(sub_score, 0) < 60 "
                        "AND added_at > ? LIMIT 50", now - 30 * 86400)
    done = 0
    for f in rows:
        age = now - (f["added_at"] or now)
        last = f["sub_checked_at"] or 0
        min_gap = 6 * 3600 if age < 14 * 86400 else 24 * 3600 if age < 90 * 86400 else 7 * 86400
        if f["sub_status"] != "pending" and now - last < min_gap - 60:
            continue
        await process_file(f["id"])
        done += 1
        await asyncio.sleep(1)
    return f"Checked {done} file(s)"


async def search_series(sid: int) -> str:
    rows = db.rows("SELECT id FROM media_files WHERE series_id = ? AND sub_status IN ('missing','pending')", sid)
    for r in rows:
        await process_file(r["id"], force=False)
    return f"Checked {len(rows)} file(s)"


async def test_provider(pid: str, cfg: dict) -> str:
    from .providers import PROVIDERS
    cls = PROVIDERS.get(pid)
    if not cls:
        raise ProviderError("Unknown provider")
    return await cls(cfg).test()
