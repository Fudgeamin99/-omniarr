"""Release-name parsing: title, season/episode, year, quality, group, language."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import quality as Q

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".wmv", ".mov", ".webm", ".mpg", ".mpeg",
              ".flv", ".divx", ".ogm", ".vob", ".iso"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}
_EXT_RE = re.compile(r"\.(mkv|mp4|avi|m4v|ts|m2ts|wmv|mov|webm|mpg|mpeg|flv|divx|ogm|vob|iso|torrent|nzb)$", re.I)

SEP = r"[\s._\-]"

# ---------------------------------------------------------------- quality ---
_RES = [
    (2160, re.compile(r"\b(2160p|4k|uhd)\b", re.I)),
    (1080, re.compile(r"\b(1080[pi]|1920x1080|fhd)\b", re.I)),
    (720, re.compile(r"\b(720p|1280x720)\b", re.I)),
    (576, re.compile(r"\b576[pi]\b", re.I)),
    (480, re.compile(r"\b(480[pi]|640x480|848x480|sd)\b", re.I)),
]
_SOURCES = [
    ("cam", re.compile(r"\b(cam|camrip|hdcam|cam-?rip)\b", re.I)),
    ("telesync", re.compile(r"\b(ts|telesync|hdts|hd-?ts|pdvd|predvdrip)\b", re.I)),
    ("telecine", re.compile(r"\b(tc|telecine|hdtc)\b", re.I)),
    ("screener", re.compile(r"\b(scr|screener|dvdscr|dvd-?scr|bdscr|webscr)\b", re.I)),
    ("workprint", re.compile(r"\b(workprint|wp)\b", re.I)),
    ("remux", re.compile(r"\b(remux|bdremux|bd-?remux)\b", re.I)),
    ("bluray", re.compile(r"\b(blu-?ray|bluray|bdrip|brrip|bd-?rip|bd25|bd50|bd|uhd-?bluray|hddvd)\b", re.I)),
    ("webrip", re.compile(r"\b(web-?rip|webrip|web\.rip|webcap|web-?cap)\b", re.I)),
    ("webdl", re.compile(r"\b(web-?dl|webdl|web\.dl|web|webhd|"
                         r"amzn|nf|netflix|dsnp|dsny|hmax|max|atvp|pcok|pmtp|hulu|stan|nowtv|itunes|"
                         r"cr|crunchyroll|funi|hidive|vrv|bcore|red|cc|ip|ipla|tving|viu)\b", re.I)),
    ("hdtv", re.compile(r"\b(hdtv|pdtv|sdtv|dsr|dsrip|dthrip|dvbrip|tvrip|hdtvrip|satrip|uhdtv)\b", re.I)),
    ("dvd", re.compile(r"\b(dvdrip|dvd-?rip|dvd|dvd5|dvd9|dvdr|ntsc|pal|xvidvd)\b", re.I)),
]
_STREAMING = re.compile(r"\b(amzn|nf|dsnp|dsny|hmax|atvp|pcok|pmtp|hulu|stan|nowtv|itunes|cr|funi|hidive|"
                        r"red|bcore|ip|crav|it)\b", re.I)
_PROPER = re.compile(r"\b(proper|repack|rerip|real)\b", re.I)
_VERSION = re.compile(r"\bv(\d)\b", re.I)
_CODEC = re.compile(r"\b(x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx|av1|vp9)\b", re.I)
_HARDSUB = re.compile(r"\b(hc|hardsub|hardsubs|hard-?coded|korsub|subbed-?in|hcsubs)\b", re.I)

# --------------------------------------------------------------- language ---
# Tokens that mean the audio is (or includes) a non-English language.
_FOREIGN = {
    "french": "fr", "truefrench": "fr", "vff": "fr", "vfq": "fr", "vf": "fr", "vf2": "fr", "vfi": "fr",
    "vostfr": "fr", "subfrench": "fr", "fre": "fr", "fra": "fr",
    "german": "de", "deutsch": "de", "ger": "de",
    "italian": "it", "ita": "it", "italiano": "it",
    "spanish": "es", "español": "es", "espanol": "es", "castellano": "es", "esp": "es", "spa": "es",
    "latino": "es", "lat": "es",
    "portuguese": "pt", "dublado": "pt", "ptbr": "pt", "pt-br": "pt", "por": "pt",
    "russian": "ru", "rus": "ru",
    "polish": "pl", "pldub": "pl", "dubpl": "pl", "lektor": "pl", "pol": "pl",
    "dutch": "nl", "flemish": "nl", "nld": "nl",
    "swedish": "sv", "swe": "sv", "danish": "da", "dan": "da", "norwegian": "no", "nor": "no",
    "finnish": "fi", "fin": "fi", "nordic": "nordic",
    "czech": "cs", "cze": "cs", "slovak": "sk", "hungarian": "hu", "hun": "hu",
    "turkish": "tr", "tur": "tr", "greek": "el", "hebrew": "he", "arabic": "ar", "ara": "ar",
    "hindi": "hi", "hin": "hi", "tamil": "ta", "tam": "ta", "telugu": "te", "tel": "te", "malayalam": "ml",
    "kannada": "kn", "bengali": "bn", "punjabi": "pa", "marathi": "mr",
    "korean": "ko", "kor": "ko", "japanese": "ja", "jap": "ja", "jpn": "ja",
    "chinese": "zh", "mandarin": "zh", "cantonese": "zh", "chs": "zh", "cht": "zh", "big5": "zh",
    "thai": "th", "vietnamese": "vi", "ukrainian": "uk", "ukr": "uk", "romanian": "ro", "rom": "ro",
    "bulgarian": "bg", "bul": "bg", "croatian": "hr", "serbian": "sr", "slovenian": "sl", "lithuanian": "lt",
    "latvian": "lv", "estonian": "et", "persian": "fa", "farsi": "fa", "indonesian": "id", "malay": "ms",
    "filipino": "tl", "tagalog": "tl",
}
# Short, ambiguous codes that only count when clearly separated and upper-case in the original.
_FOREIGN_UPPER_ONLY = {"FR": "fr", "DE": "de", "IT": "it", "ES": "es", "PL": "pl", "NL": "nl", "RU": "ru",
                       "PT": "pt", "SWE": "sv", "KOR": "ko", "JPN": "ja", "CN": "zh", "HUN": "hu", "CZ": "cs",
                       "TR": "tr", "UA": "uk", "HE": "he"}
_ENGLISH = {"english", "eng", "en", "engsub"}  # 'engsub' is subtitles; handled separately
_DUB = re.compile(r"\b(dub|dubbed|english[\s._-]?dub|eng[\s._-]?dub|en[\s._-]?dub|dubs)\b", re.I)
_DUAL = re.compile(r"\b(dual[\s._-]?audio|dual|multi[\s._-]?audio|multi[\s._-]?dub|tri[\s._-]?audio|"
                   r"multi|multilang|multi[\s._-]?lang|dl)\b", re.I)
_SUBBED = re.compile(r"\b(sub|subs|subbed|eng[\s._-]?sub(s|bed)?|english[\s._-]?sub(s|bed)?|vostfr|"
                     r"softsub|softsubs)\b", re.I)
_RAW = re.compile(r"\b(raw|raws)\b", re.I)


@dataclass
class Parsed:
    raw: str
    title: str = ""                 # series or movie title as written
    year: int | None = None
    seasons: list[int] = field(default_factory=list)
    episodes: list[int] = field(default_factory=list)
    absolute: list[int] = field(default_factory=list)
    full_season: bool = False
    multi_season: bool = False
    complete_series: bool = False
    special: bool = False
    air_date: str | None = None
    resolution: int | None = None
    source: str | None = None
    quality: str = "Unknown"
    revision: int = 1               # 2+ for proper/repack
    codec: str | None = None
    group: str | None = None
    streaming: str | None = None
    languages: set = field(default_factory=set)   # non-English language codes found
    has_english_tag: bool = False
    dual_audio: bool = False
    dubbed: bool = False
    subbed: bool = False
    raw_release: bool = False
    hardcoded_subs: bool = False
    anime_style: bool = False

    @property
    def is_tv(self) -> bool:
        return bool(self.seasons or self.episodes or self.absolute or self.air_date or self.complete_series)

    @property
    def rejected_source(self) -> bool:
        return self.source in Q.REJECTED_SOURCES

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["languages"] = sorted(self.languages)
        return d


def strip_ext(name: str) -> str:
    return _EXT_RE.sub("", name.strip())


def normalize_title(s: str) -> str:
    """Lower-case, ASCII-fold, & → and, drop punctuation, collapse spaces."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ").replace("+", " and ")
    s = re.sub(r"['’`´]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_title(s: str) -> str:
    s = re.sub(r"^\s*(\[[^\]]*\]|\([^)]*\)|【[^】]*】)\s*", "", s)    # leading [group]
    if "." in s and " " not in s.strip():
        s = s.replace(".", " ")
    s = s.replace("_", " ")
    s = re.sub(r"(?<=\w)\.(?=\w{2,})", " ", s)                 # dots between words
    s = re.sub(r"\s+", " ", s).strip(" -._[](")
    return s


_YEAR_TAIL = re.compile(r"[\s._(\[]+((?:19|20)\d{2})[)\]]?$")


def _split_title_year(t: str) -> tuple[str, int | None]:
    t = clean_title(t)
    m = _YEAR_TAIL.search(t)
    if m and m.start() > 0:
        return t[:m.start()].strip(" -._(["), int(m.group(1))
    return t, None


_TV_PATTERNS = [
    # S01E01, S01E01E02, S01E01-E03, S01E01-03, S01.E01
    re.compile(rf"^(?P<title>.*?){SEP}*\bS(?P<season>\d{{1,2}}){SEP}?E(?P<ep>\d{{1,4}})"
               rf"(?P<more>(?:(?:{SEP}?|-|\+|&)E?\d{{1,4}}\b(?!p))*)", re.I),
    # 1x01, 1x01-1x02
    re.compile(rf"^(?P<title>.*?){SEP}+(?P<season>\d{{1,2}})x(?P<ep>\d{{2,3}})"
               rf"(?P<more>(?:[x-]\d{{2,3}}|-\d{{1,2}}x\d{{2,3}})*)\b", re.I),
]
_MULTI_SEASON = re.compile(rf"^(?P<title>.*?){SEP}+(?:S(?P<a>\d{{1,2}}){SEP}?(?:-|to|~){SEP}?S?(?P<b>\d{{1,2}})|"
                           rf"Seasons?{SEP}?(?P<c>\d{{1,2}}){SEP}?(?:-|to|~|&){SEP}?(?P<d>\d{{1,2}}))\b", re.I)
_SEASON = re.compile(rf"^(?P<title>.*?){SEP}+(?:S(?P<s1>\d{{1,2}})(?!{SEP}?E\d)(?:{SEP}?(?:complete|pack|full))?|"
                     rf"Season{SEP}?(?P<s2>\d{{1,2}})(?!{SEP}?(?:E|Episode)\d))\b", re.I)
_DAILY = re.compile(rf"^(?P<title>.*?){SEP}+(?P<y>(?:19|20)\d{{2}}){SEP}(?P<m>0[1-9]|1[0-2]){SEP}"
                    rf"(?P<d>0[1-9]|[12]\d|3[01])\b")
_COMPLETE = re.compile(rf"^(?P<title>.*?){SEP}+(?:the{SEP}+)?complete(?:{SEP}+series)?\b", re.I)
_ANIME = re.compile(r"^(?:\[(?P<group>[^\]]+)\][\s._]*)(?P<title>.+?)[\s._]+(?:-[\s._]+)?(?:E|EP|Episode[\s._]?|#)?"
                    r"(?P<abs>\d{1,4})(?:v\d)?(?:[\s._]*[-~][\s._]*(?P<abs2>\d{1,4})(?:v\d)?)?"
                    r"(?=[\s._]*(?:\[|\(|$|END|v\d))", re.I)
_ANIME_NOGROUP = re.compile(r"^(?P<title>.+?)[\s._]+-[\s._]+(?P<abs>\d{2,4})(?:v\d)?(?=[\s._]*(?:\[|\(|$))", re.I)
_MOVIE_YEAR = re.compile(r"(?<![\w])[(\[]?((?:19|20)\d{2})[)\]]?(?![\w])")


def _episodes_from_more(first: int, more: str) -> list[int]:
    eps = [first]
    if not more:
        return eps
    nums = [int(n) for n in re.findall(r"\d{1,4}", more)]
    is_range = bool(re.match(r"\s*-", more)) and len(nums) == 1
    for n in nums:
        if n <= eps[-1]:
            continue
        if is_range and n - eps[-1] < 60:
            eps.extend(range(eps[-1] + 1, n + 1))
        else:
            eps.append(n)
    return eps


def parse(name: str) -> Parsed:
    raw = name
    base = strip_ext(name.split("/")[-1].split("\\")[-1])
    p = Parsed(raw=raw)
    _parse_quality(base, p)
    _parse_group(base, p)
    p._base = base

    work = base
    # TV patterns
    for pat in _TV_PATTERNS:
        m = pat.search(work)
        if m and m.group("title").strip(" .-_"):
            p.seasons = [int(m.group("season"))]
            first = int(m.group("ep"))
            if pat is _TV_PATTERNS[1]:
                more = re.sub(r"\d{1,2}x", "", m.group("more") or "")
            else:
                more = m.group("more") or ""
            p.episodes = _episodes_from_more(first, more)
            p.title, p.year = _split_title_year(m.group("title"))
            p.special = p.seasons == [0]
            return _finish(p, m.end("title"))
    m = _MULTI_SEASON.search(work)
    if m and m.group("title").strip():
        a, b = (m.group("a"), m.group("b")) if m.group("a") else (m.group("c"), m.group("d"))
        a, b = int(a), int(b)
        if 0 < b - a < 50:
            p.seasons = list(range(a, b + 1))
            p.full_season = True
            p.multi_season = True
            p.title, p.year = _split_title_year(m.group("title"))
            return _finish(p, m.end("title"))
    m = _SEASON.search(work)
    if m and m.group("title").strip():
        p.seasons = [int(m.group("s1") or m.group("s2"))]
        p.full_season = True
        p.title, p.year = _split_title_year(m.group("title"))
        return _finish(p, m.end("title"))
    m = _DAILY.search(work)
    if m and m.group("title").strip():
        p.air_date = f"{m.group('y')}-{m.group('m')}-{m.group('d')}"
        p.title, p.year = _split_title_year(m.group("title"))
        return _finish(p, m.end("title"))
    m = _ANIME.search(work) or _ANIME_NOGROUP.search(work)
    if m and not re.fullmatch(r"(19|20)\d{2}", m.group("abs")):
        n = int(m.group("abs"))
        if n not in (480, 576, 720, 1080, 2160, 264, 265):
            p.anime_style = True
            p.absolute = [n]
            if m.groupdict().get("abs2"):
                n2 = int(m.group("abs2"))
                if n < n2 < n + 200:
                    p.absolute = list(range(n, n2 + 1))
            p.title, p.year = _split_title_year(m.group("title"))
            if m.groupdict().get("group"):
                p.group = m.group("group")
            return _finish(p, m.end("title"))
    m = _COMPLETE.search(work)
    if m and m.group("title").strip() and not _MOVIE_YEAR.search(m.group("title")[-6:] or ""):
        p.complete_series = True
        p.full_season = True
        p.title, p.year = _split_title_year(m.group("title"))
        return _finish(p, m.end("title"))

    # Movie: title up to the last plausible year
    years = [y for y in _MOVIE_YEAR.finditer(work) if y.start() > 0]
    if years:
        y = years[-1]
        # "2001 A Space Odyssey 1968": prefer last; but "Blade Runner 2049 2017": also last
        p.title = clean_title(work[:y.start()])
        p.year = int(y.group(1))
        if not p.title and len(years) > 1:
            y = years[-2]
            p.title, p.year = clean_title(work[:y.start()]), int(y.group(1))
        return _finish(p, y.start())
    # no year: cut at first quality-ish token
    cut = re.search(r"[\s._\[(](2160p|1080p|720p|480p|web|bluray|hdtv|dvdrip|x264|x265|hevc|remux)\b",
                    work, re.I)
    p.title = clean_title(work[:cut.start()] if cut else work)
    return _finish(p, cut.start() if cut else len(work))


def _finish(p: Parsed, tail_start: int = 0) -> Parsed:
    p.title = re.sub(r"\s+", " ", p.title).strip(" -.")
    base = getattr(p, "_base", p.raw)
    _parse_language(base, p, tail_start, lenient=not p.is_tv)
    if p.anime_style and not p.source and p.resolution:
        p.source = "webdl"
    p.quality = Q.quality_name(p.source, p.resolution) if not p.rejected_source else "Unknown"
    return p


def _parse_quality(s: str, p: Parsed) -> None:
    for res, rx in _RES:
        if rx.search(s):
            p.resolution = res
            break
    tokens = re.sub(r"[\[\]()]", " ", s)
    for src, rx in _SOURCES:
        if rx.search(tokens):
            # "TS" is also a container extension and a common word in titles; require HD resolution absent
            if src == "telesync" and re.search(r"\bts\b", tokens, re.I) and not re.search(
                    r"\b(telesync|hdts|hd-?ts|pdvd)\b", tokens, re.I):
                if p.resolution and p.resolution >= 720 and re.search(
                        r"\b(web|bluray|hdtv|webrip|web-?dl)\b", tokens, re.I):
                    continue
            if src == "telecine" and not re.search(r"\b(telecine|hdtc)\b", tokens, re.I):
                # bare "TC" only counts if no other source is present
                if any(rx2.search(tokens) for s2, rx2 in _SOURCES if s2 in ("webdl", "webrip", "bluray", "hdtv")):
                    continue
            if src == "workprint" and not re.search(r"\bworkprint\b", tokens, re.I):
                if any(rx2.search(tokens) for s2, rx2 in _SOURCES if s2 in ("webdl", "webrip", "bluray", "hdtv")):
                    continue
            if src == "bluray" and re.fullmatch(r"(?i).*\bbd\b.*", tokens) and not re.search(
                    r"(?i)\b(blu-?ray|bdrip|brrip|bd-?rip|bd25|bd50)\b", tokens):
                # bare "BD" is common in anime (BD = Blu-ray) — keep it, but only if no web token
                if re.search(r"(?i)\b(web|web-?dl|webrip)\b", tokens):
                    continue
            if src == "webdl":
                m = re.search(r"(?i)\b(web-?dl|webdl|web\.dl|web)\b", tokens)
                if not m and not p.resolution:
                    continue  # a bare streaming tag without resolution is too weak
            p.source = src
            break
    m = _STREAMING.search(tokens)
    if m and p.source in ("webdl", "webrip"):
        p.streaming = m.group(1).upper()
    if _PROPER.search(s):
        p.revision = 2 + len(_PROPER.findall(s)) - 1
    v = _VERSION.search(s)
    if v and int(v.group(1)) > 1:
        p.revision = max(p.revision, int(v.group(1)))
    c = _CODEC.search(s)
    if c:
        p.codec = c.group(1).lower().replace(".", "")
    p.hardcoded_subs = bool(_HARDSUB.search(s))


_QUALITY_TOKEN = re.compile(r"(?i)^(2160p|1080p|1080i|720p|576p|480p|4k|uhd|web|web-?dl|webdl|webrip|bluray|"
                            r"blu-?ray|bdrip|brrip|hdtv|dvdrip|remux|x264|x265|h264|h265|hevc|avc|xvid|hdr|dv)$")


def _parse_language(s: str, p: Parsed, tail_start: int = 0, lenient: bool = False) -> None:
    """Look for language markers after the title (episode names are skipped unless clearly tags)."""
    body = s[tail_start:] if tail_start else re.sub(r"^\s*\[[^\]]*\]", " ", s)
    bracketed = " ".join(re.findall(r"[\[(]([^\])]*)[\])]", body))
    bracket_tokens = {t.lower() for t in re.split(r"[\s._\-+,/|]+", bracketed) if t}
    tokens = [t for t in re.split(r"[\s._\-\[\]()+,/|]+", body) if t]
    qpos = next((i for i, t in enumerate(tokens) if _QUALITY_TOKEN.match(t)), None)
    for i, t in enumerate(tokens):
        tl = t.lower()
        strong = lenient or t.isupper() or (qpos is not None and i > qpos) or tl in bracket_tokens
        if tl in _FOREIGN and (strong or (len(tl) > 3 and t[0].isupper() and qpos is not None and i >= qpos - 1)):
            p.languages.add(_FOREIGN[tl])
        elif t in _FOREIGN_UPPER_ONLY and (qpos is None or i > qpos or tl in bracket_tokens):
            p.languages.add(_FOREIGN_UPPER_ONLY[t])
        if (tl in ("english", "eng") and strong) or t == "EN":
            p.has_english_tag = True
    p.dubbed = bool(_DUB.search(body))
    p.dual_audio = bool(_DUAL.search(body))
    # "DL" alone is ambiguous (WEB-DL); only count it after a language token, e.g. German.DL
    if p.dual_audio and not re.search(r"(?i)\b(dual|multi|tri)", body):
        if not re.search(r"(?i)\b(german|french|ita|spanish|polish|nordic)[\s._-]+dl\b", body):
            p.dual_audio = False
    p.subbed = bool(_SUBBED.search(body))
    p.raw_release = bool(_RAW.search(body))
    # "VOSTFR" = original audio with French subs: not French audio
    if re.search(r"(?i)\bvostfr\b", body):
        p.languages.discard("fr")
        p.subbed = True


_GROUP_RE = re.compile(r"-(?P<group>[A-Za-z0-9][A-Za-z0-9_.]{1,24})(?:\[[^\]]*\])?(?:\s*\([^)]*\))?$")
_BAD_GROUPS = {"dl", "web", "rip", "264", "265", "hd", "sd", "4k", "dts", "ac3", "aac", "atmos", "hdr", "dv",
               "x264", "x265", "hevc", "1080p", "720p", "2160p", "eng", "esub", "esubs", "multi"}


def _parse_group(s: str, p: Parsed) -> None:
    m = re.match(r"^\[(?P<g>[^\]]+)\]", s)
    if m:
        p.group = m.group("g").strip()
        return
    m = _GROUP_RE.search(s.strip())
    if m and m.group("group").lower() not in _BAD_GROUPS and not m.group("group").isdigit():
        p.group = m.group("group").strip(".")


# ---------------------------------------------------------------------------
def language_decision(p: Parsed, original_language: str | None, mode: str, is_anime: bool,
                      prefer_dub: bool) -> tuple[bool, str, int]:
    """Decide whether a release's audio is acceptable.

    Returns (accepted, reason, preference score). Higher score wins ties.
    """
    orig = (original_language or "en").lower()[:2]
    orig_english = orig == "en"
    foreign = {l for l in p.languages if l not in ("en",)}
    has_english_audio: bool
    if orig_english:
        # English original: untagged releases are English. Foreign-only tags mean a foreign dub.
        if foreign and not (p.has_english_tag or p.dual_audio):
            has_english_audio = False
        else:
            has_english_audio = True
    else:
        # Non-English original (anime, foreign film): needs an explicit English audio signal.
        has_english_audio = bool(p.dubbed or p.dual_audio or (p.has_english_tag and not p.subbed))
        if p.raw_release:
            has_english_audio = False

    score = 0
    if not orig_english and has_english_audio:
        score += 2 if (is_anime and prefer_dub) else 1
        if p.dual_audio:
            score += 1
    if mode == "english" and not has_english_audio:
        if orig_english:
            langs = ", ".join(sorted(foreign)) or "foreign"
            return False, f"Not English audio ({langs})", score
        return False, "No English audio (original language, subtitles only)", score
    return True, "", score
