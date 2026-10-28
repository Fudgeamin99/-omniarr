"""Shared release model and value parsing helpers."""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import asdict, dataclass, field

from dateutil import parser as dateparser

NEWZNAB_CATS = {
    "Movies": 2000, "Movies/Foreign": 2010, "Movies/Other": 2020, "Movies/SD": 2030, "Movies/HD": 2040,
    "Movies/UHD": 2045, "Movies/BluRay": 2050, "Movies/3D": 2060, "Movies/DVD": 2070, "Movies/WEB-DL": 2080,
    "Movies/x265": 2090,
    "TV": 5000, "TV/WEB-DL": 5010, "TV/Foreign": 5020, "TV/SD": 5030, "TV/HD": 5040, "TV/UHD": 5045,
    "TV/Other": 5050, "TV/Sport": 5060, "TV/Anime": 5070, "TV/Documentary": 5080, "TV/x265": 5090,
}


def cat_kind(cat: int | str | None) -> str | None:
    if cat is None or cat == "":
        return None
    try:
        n = int(cat)
    except (TypeError, ValueError):
        s = str(cat)
        if s.startswith("TV"):
            return "tv"
        if s.startswith("Movies"):
            return "movies"
        return "other"
    if 5000 <= n < 6000 or 105000 <= n < 106000:
        return "tv"
    if 2000 <= n < 3000 or 102000 <= n < 103000:
        return "movies"
    return "other"


@dataclass
class Release:
    title: str
    indexer_id: int
    indexer_name: str
    guid: str = ""
    download_url: str | None = None
    magnet: str | None = None
    info_hash: str | None = None
    details_url: str | None = None
    size: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    grabs: int | None = None
    files: int | None = None
    published: float | None = None
    categories: list = field(default_factory=list)   # newznab ids
    imdb_id: str | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    min_ratio: float | None = None
    min_seed_time: int | None = None                  # seconds
    download_factor: float | None = None
    indexer_priority: int = 25

    @property
    def kinds(self) -> set:
        return {k for k in (cat_kind(c) for c in self.categories) if k}

    def to_dict(self) -> dict:
        return asdict(self)


_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
          "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4,
          "ko": 1000, "mo": 1000 ** 2, "go": 1000 ** 3, "to": 1000 ** 4,
          "кб": 1024, "мб": 1024 ** 2, "гб": 1024 ** 3, "тб": 1024 ** 4,
          "bytes": 1, "byte": 1}


def parse_size(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace("\xa0", " ")
    if not s:
        return None
    if re.fullmatch(r"\d+", s):
        return int(s)
    m = re.search(r"([\d.,\s]+)\s*([a-zA-Zкбмгт]+)", s, re.I)
    if not m:
        try:
            return int(float(s.replace(",", "")))
        except ValueError:
            return None
    num, unit = m.group(1).strip().replace(" ", ""), m.group(2).lower()
    # "1,234.5" vs "1.234,5" vs "1,5"
    if "," in num and "." in num:
        num = num.replace(",", "") if num.rfind(".") > num.rfind(",") else num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", ".") if len(num.split(",")[-1]) != 3 else num.replace(",", "")
    try:
        f = float(num)
    except ValueError:
        return None
    mult = _UNITS.get(unit) or _UNITS.get(unit.rstrip("s")) or 1
    return int(f * mult)


def parse_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower().replace(",", "").replace(" ", "").replace("\xa0", "")
    m = re.search(r"(-?\d+(?:\.\d+)?)(k|m)?", s)
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2) == "k":
        n *= 1000
    elif m.group(2) == "m":
        n *= 1_000_000
    return int(n)


def parse_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).strip().replace(",", "."))
    except ValueError:
        return None


_REL = re.compile(r"(\d+(?:[.,]\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|"
                  r"hours|d|day|days|w|wk|week|weeks|mo|mon|month|months|y|yr|year|years)\b", re.I)
_UNIT_SECS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600,
              "hour": 3600, "d": 86400, "day": 86400, "w": 604800, "wk": 604800, "week": 604800,
              "mo": 2592000, "mon": 2592000, "month": 2592000, "y": 31536000, "yr": 31536000, "year": 31536000}


def parse_relative(s: str, now: float | None = None) -> float | None:
    now = now or time.time()
    t = s.strip().lower()
    if not t:
        return None
    if t in ("now", "just now", "right now") or "moment" in t:
        return now
    base = now
    m = re.search(r"(today|yesterday|tomorrow)", t)
    if m:
        day = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        day += dt.timedelta(days={"today": 0, "yesterday": -1, "tomorrow": 1}[m.group(1)])
        hm = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?", t)
        if hm:
            h = int(hm.group(1)) % 12 + (12 if hm.group(4) == "pm" else 0) if hm.group(4) else int(hm.group(1))
            day = day.replace(hour=h, minute=int(hm.group(2)), second=int(hm.group(3) or 0))
        return day.timestamp()
    total = 0.0
    for num, unit in _REL.findall(t):
        u = unit.lower()
        u = u if u in _UNIT_SECS else u.rstrip("s")
        total += float(num.replace(",", ".")) * _UNIT_SECS.get(u, 0)
    if total:
        return base - total
    return None


_NET_TOKENS = [("yyyy", "%Y"), ("yy", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("M", "%m"),
               ("dddd", "%A"), ("ddd", "%a"), ("dd", "%d"), ("d", "%d"), ("HH", "%H"), ("H", "%H"),
               ("hh", "%I"), ("h", "%I"), ("mm", "%M"), ("ss", "%S"), ("fff", "%f"), ("tt", "%p"),
               ("zzz", "%z"), ("zz", "%z"), ("K", "%z")]


def net_format_to_strptime(fmt: str) -> str:
    out, i = [], 0
    while i < len(fmt):
        if fmt[i] in "'\"":
            j = fmt.find(fmt[i], i + 1)
            j = len(fmt) if j < 0 else j
            out.append(fmt[i + 1:j].replace("%", "%%"))
            i = j + 1
            continue
        for tok, rep in _NET_TOKENS:
            if fmt.startswith(tok, i):
                out.append(rep)
                i += len(tok)
                break
        else:
            out.append("%%" if fmt[i] == "%" else fmt[i])
            i += 1
    return "".join(out)


def parse_date_fmt(value: str, fmt: str) -> float | None:
    v = value.strip()
    pyfmt = net_format_to_strptime(fmt)
    # Python's %z wants +hh:mm or +hhmm; .NET "zzz" gives +hh:mm — fine. Allow missing tz.
    for candidate in (pyfmt, pyfmt.replace(" %z", "").replace("%z", "")):
        try:
            d = dt.datetime.strptime(v, candidate)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.timestamp()
        except ValueError:
            continue
    return parse_date(v)


def parse_date(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        return n / 1000 if n > 1e11 else n
    s = str(v).strip()
    if re.fullmatch(r"\d{9,13}", s):
        n = float(s)
        return n / 1000 if n > 1e11 else n
    rel = parse_relative(s) if re.search(r"(?i)ago|today|yesterday|now|\b(min|hour|day|week|month|year)s?\b", s) \
        else None
    if rel:
        return rel
    try:
        d = dateparser.parse(s, fuzzy=True)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except (ValueError, OverflowError, TypeError):
        return None


def normalize_imdb(v) -> str | None:
    if not v:
        return None
    m = re.search(r"(?:tt)?(\d{5,9})", str(v))
    return f"tt{int(m.group(1)):07d}" if m else None
