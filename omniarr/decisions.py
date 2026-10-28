"""Decide whether a release is wanted, and rank the acceptable ones."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from . import db, library, parser, quality, settings
from .indexers.base import Release


@dataclass
class Decision:
    release: Release
    parsed: parser.Parsed
    kind: str                                  # episode | movie
    accepted: bool = True
    rejections: list[str] = field(default_factory=list)
    series_id: int | None = None
    movie_id: int | None = None
    episode_ids: list[int] = field(default_factory=list)
    season_pack: bool = False
    lang_score: int = 0
    upgrade: bool = False

    def reject(self, why: str) -> None:
        self.accepted = False
        if why not in self.rejections:
            self.rejections.append(why)

    def score(self) -> tuple:
        r = self.release
        seeds = r.seeders if r.seeders is not None else 5
        bucket = int(math.log2(seeds + 1)) if seeds > 0 else -1
        return (quality.tier(self.parsed.quality), self.parsed.revision, self.lang_score, min(bucket, 8),
                1 if (r.download_factor is not None and r.download_factor < 1) else 0,
                -(r.indexer_priority or 25), seeds, r.published or 0)

    def to_dict(self) -> dict:
        r = self.release
        return {
            "title": r.title, "indexer": r.indexer_name, "indexer_id": r.indexer_id, "size": r.size,
            "seeders": r.seeders, "leechers": r.leechers, "published": r.published, "quality": self.parsed.quality,
            "languages": sorted(self.parsed.languages), "dual_audio": self.parsed.dual_audio,
            "dubbed": self.parsed.dubbed, "accepted": self.accepted, "rejections": self.rejections,
            "season_pack": self.season_pack, "episodes": len(self.episode_ids), "details_url": r.details_url,
            "freeleech": r.download_factor == 0, "upgrade": self.upgrade,
        }


# ------------------------------------------------------------------ context --
@dataclass
class Context:
    queued_episodes: set
    queued_movies: set
    blocked_titles: set
    blocked_hashes: set
    file_quality: dict      # file_id -> (quality, revision)

    @classmethod
    def load(cls) -> "Context":
        q_eps, q_movies = set(), set()
        for d in db.rows("SELECT * FROM downloads WHERE status IN ('downloading','importing','import_blocked')"):
            if d["kind"] == "episode":
                q_eps.update(db.jload(d["episode_ids"], []))
            elif d["movie_id"]:
                q_movies.add(d["movie_id"])
        bl = db.rows("SELECT title, hash FROM blocklist")
        fq = {f["id"]: (f["quality"], f["revision"] or 1) for f in db.rows("SELECT id, quality, revision FROM media_files")}
        return cls(q_eps, q_movies, {(b["title"] or "").lower() for b in bl},
                   {(b["hash"] or "").lower() for b in bl if b["hash"]}, fq)


# ----------------------------------------------------------------- matching --
def series_names(s: dict) -> set[str]:
    base = parser.normalize_title(s["title"])
    names = {base}
    no_year = re.sub(r"\s(19|20)\d{2}$", "", base)
    names.add(no_year)
    if s.get("year"):
        names.add(f"{no_year} {s['year']}")
    cc = (s.get("country") or "").lower()
    if cc:
        names.add(f"{no_year} {'uk' if cc == 'gb' else cc}")
    for a in db.jload(s.get("aliases"), []) or []:
        names.add(parser.normalize_title(a))
    out = set()
    for n in names:
        out.add(n)
        out.add(n.replace(" ", ""))
        if n.startswith("the "):
            out.add(n[4:])
            out.add(n[4:].replace(" ", ""))
        out.add(n.replace(" and ", " "))
    return {n for n in out if n}


def title_matches_series(p: parser.Parsed, s: dict, names: set | None = None) -> bool:
    t = parser.normalize_title(p.title)
    names = names or series_names(s)
    return t in names or t.replace(" ", "") in names or (t.startswith("the ") and t[4:] in names)


def movie_names(m: dict) -> set[str]:
    names = {parser.normalize_title(m["title"])}
    if m.get("original_title"):
        names.add(parser.normalize_title(m["original_title"]))
    for a in db.jload(m.get("aliases"), []) or []:
        names.add(parser.normalize_title(a))
    out = set()
    for n in names:
        out |= {n, n.replace(" ", ""), n.replace(" and ", " ")}
        if n.startswith("the "):
            out.add(n[4:])
    return {n for n in out if n}


def title_matches_movie(p: parser.Parsed, m: dict) -> bool:
    t = parser.normalize_title(p.title)
    names = movie_names(m)
    if t not in names and t.replace(" ", "") not in names:
        return False
    if p.year and m.get("year") and abs(p.year - m["year"]) > 1:
        return False
    return True


# ------------------------------------------------------------ common checks --
def _common(d: Decision, prof_id: str, language_mode: str, orig_lang: str | None, is_anime: bool,
            ctx: Context, runtime_min: int | None, units: int) -> None:
    p, r = d.parsed, d.release
    srch = settings.get("search")
    lang_cfg = settings.get("language")
    if p.rejected_source:
        d.reject("Cam / pre-release recording")
    prof = quality.profile(prof_id)
    if p.quality not in prof.allowed:
        d.reject(f"{p.quality} isn't wanted in the {prof.name} profile")
    mode = lang_cfg["mode"] if language_mode in (None, "", "default") else language_mode
    ok, why, score = parser.language_decision(p, orig_lang, mode, is_anime, lang_cfg.get("anime_prefer_dub", True))
    d.lang_score = score
    if not ok:
        d.reject(why)
    if srch.get("reject_hardcoded_subs") and p.hardcoded_subs:
        d.reject("Hard-coded subtitles")
    for w in srch.get("ignored_words") or []:
        if w and w.lower() in r.title.lower():
            d.reject(f"Contains ignored word '{w}'")
    min_seeds = int(srch.get("min_seeders") or 0)
    if r.seeders is not None and r.seeders < min_seeds:
        d.reject(f"Not enough seeders ({r.seeders})")
    if r.size and runtime_min and p.quality in quality.BY_NAME:
        lo, hi = quality.size_limits(p.quality)
        per_min = r.size / 1_000_000 / max(1, units) / max(1, runtime_min)
        if lo and per_min < lo:
            d.reject("Too small for its quality — probably fake or very low bitrate")
        if hi and per_min > hi:
            d.reject("Too large")
    if r.title.lower() in ctx.blocked_titles or (r.info_hash and r.info_hash.lower() in ctx.blocked_hashes):
        d.reject("Blocked (failed before)")


def _upgrade_ok(d: Decision, file_q: tuple[str, int] | None, prof_id: str) -> bool:
    if not file_q:
        return True
    cur_q, cur_rev = file_q
    prof = quality.profile(prof_id)
    new_t, cur_t = quality.tier(d.parsed.quality), quality.tier(cur_q)
    if cur_t >= quality.tier(prof.cutoff) and not (new_t == cur_t and d.parsed.revision > cur_rev):
        return False
    return new_t > cur_t or (new_t == cur_t and d.parsed.revision > cur_rev)


# ----------------------------------------------------------------------- tv --
def evaluate_tv(rel: Release, s: dict, episodes: list[dict], ctx: Context, *, parsed: parser.Parsed | None = None,
                names: set | None = None, require_title: bool = True) -> Decision:
    p = parsed or parser.parse(rel.title)
    d = Decision(rel, p, "episode", series_id=s["id"])
    if not p.is_tv:
        d.reject("Not a TV episode or season")
        return d
    if require_title and not title_matches_series(p, s, names):
        d.reject("Different show")
        return d
    if p.year and s.get("year") and abs(p.year - s["year"]) > 1:
        d.reject(f"Different show ({p.year})")
        return d
    by_key = {(e["season"], e["number"]): e for e in episodes}
    targets: list[dict] = []
    if p.seasons and p.episodes:
        targets = [by_key[(p.seasons[0], n)] for n in p.episodes if (p.seasons[0], n) in by_key]
    elif p.complete_series:
        targets = [e for e in episodes if e["season"] > 0]
        d.season_pack = True
    elif p.full_season and p.seasons:
        targets = [e for e in episodes if e["season"] in p.seasons]
        d.season_pack = True
    elif p.air_date:
        targets = [e for e in episodes if e.get("airdate") == p.air_date]
    elif p.absolute:
        by_abs = {e["absolute"]: e for e in episodes if e.get("absolute")}
        targets = [by_abs[n] for n in p.absolute if n in by_abs]
    if not targets:
        d.reject("Episode isn't in this show's episode list")
        return d
    d.episode_ids = [e["id"] for e in targets]
    runtime = targets[0].get("runtime") or s.get("runtime")
    _common(d, s["quality_profile"], s["language_mode"], s.get("original_language"), bool(s.get("is_anime")),
            ctx, runtime, len(targets))

    if d.season_pack:
        unaired = [e for e in targets if not library.aired(e)]
        if unaired:
            d.reject("Season hasn't finished airing")
    monitored = [e for e in targets if e["monitored"]]
    if not s["monitored"] or not monitored:
        d.reject("Not monitored")
        return d
    if any(e["id"] in ctx.queued_episodes for e in targets):
        d.reject("Already downloading")
    needed = []
    for e in monitored:
        fq = ctx.file_quality.get(e["file_id"]) if e.get("file_id") else None
        if not library.aired(e, grace_hours=12):
            continue
        if _upgrade_ok(d, fq, s["quality_profile"]):
            needed.append(e)
            if fq:
                d.upgrade = True
    if not needed:
        d.reject("Already have it at this quality or better")
    elif d.season_pack:
        # a pack must not re-download episodes we already have at good quality
        have_good = [e for e in monitored if e not in needed and e.get("file_id")]
        if have_good and len(have_good) > len(monitored) // 2:
            d.reject("Season pack — you already have most of this season")
    return d


# ------------------------------------------------------------------- movies --
def evaluate_movie(rel: Release, m: dict, ctx: Context, *, parsed: parser.Parsed | None = None,
                   require_title: bool = True) -> Decision:
    p = parsed or parser.parse(rel.title)
    d = Decision(rel, p, "movie", movie_id=m["id"])
    if p.is_tv:
        d.reject("This is a TV release")
        return d
    if require_title and not title_matches_movie(p, m):
        imdb_ok = rel.imdb_id and m.get("imdb_id") and rel.imdb_id == m["imdb_id"]
        if not imdb_ok:
            d.reject("Different movie")
            return d
    _common(d, m["quality_profile"], m["language_mode"], m.get("original_language"), False, ctx,
            m.get("runtime") or 110, 1)
    if not m["monitored"]:
        d.reject("Not monitored")
    if not library.movie_available(m):
        d.reject("Not released for home viewing yet")
    if m["id"] in ctx.queued_movies:
        d.reject("Already downloading")
    fq = ctx.file_quality.get(m["file_id"]) if m.get("file_id") else None
    if not _upgrade_ok(d, fq, m["quality_profile"]):
        d.reject("Already have it at this quality or better")
    elif fq:
        d.upgrade = True
    return d


def best(decisions: list[Decision]) -> list[Decision]:
    """Accepted decisions, best first."""
    return sorted([d for d in decisions if d.accepted], key=lambda d: d.score(), reverse=True)
