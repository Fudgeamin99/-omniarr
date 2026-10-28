"""Cardigann engine: runs Prowlarr/Jackett YAML site definitions."""
from __future__ import annotations

import copy
import datetime as dt
import html as _html
import json
import re
import time
import unicodedata
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .. import log
from ..net import IndexerError, Resp, Session
from . import base as B
from .netregex import compile_net, net_replacement
from .template import render

L = log.get("cardigann")


# ----------------------------------------------------------------- selectors --
def css(sel: str) -> str:
    """jQuery/AngleSharp selector quirks → soupsieve."""
    s = sel.strip()
    # unquoted attribute values that aren't identifiers: [border=1] → [border="1"]
    s = re.sub(r"\[([\w-]+)\s*([~|^$*]?=)\s*([^\]\"'\s]+)\]", r'[\1\2"\3"]', s)
    s = re.sub(r":contains\(", ":-soup-contains(", s)
    s = re.sub(r":containsOwn\(", ":-soup-contains-own(", s)
    # :eq(n) (jQuery, zero-based) → nth-of-type approximation
    s = re.sub(r":eq\((\d+)\)", lambda m: f":nth-of-type({int(m.group(1)) + 1})", s)
    s = re.sub(r":gt\((\d+)\)", lambda m: f":nth-of-type(n+{int(m.group(1)) + 2})", s)
    s = re.sub(r":lt\((\d+)\)", lambda m: f":nth-of-type(-n+{int(m.group(1))})", s)
    s = s.replace(":first", ":first-of-type") if re.search(r":first(?![-\w])", s) else s
    s = s.replace(":last", ":last-of-type") if re.search(r":last(?![-\w])", s) else s
    return s


def select_all(node, sel: str) -> list:
    if not sel:
        return [node]
    try:
        return node.select(css(sel))
    except Exception as e:
        raise IndexerError("parse", f"Selector not supported: {sel} ({e})") from e


def select_one(node, sel: str):
    if not sel:
        return node
    try:
        return node.select_one(css(sel))
    except Exception as e:
        raise IndexerError("parse", f"Selector not supported: {sel} ({e})") from e


def matches(node, sel: str) -> bool:
    try:
        return bool(node.select_one(css(sel))) or node.parent is not None and node in node.parent.select(css(sel))
    except Exception:
        return False


def text_of(el) -> str:
    if el is None:
        return ""
    if isinstance(el, Tag):
        return el.get_text("", strip=False).strip()
    return str(el).strip()


# ------------------------------------------------------------- json "selectors"
def json_select(data: Any, path: str, parent: Any = None) -> Any:
    """Tiny SelectToken: 'a.b[0].c', '$', '$[0].id', '..parentKey', filters ':has(..)' ignored."""
    p = (path or "").strip()
    p = re.sub(r":(has|not)\(.*\)$", "", p)
    if p in ("", "$", "."):
        return data
    if p.startswith(".."):
        return json_select(parent, p[2:]) if parent is not None else None
    if p.startswith("$"):
        p = p[1:]
    cur = data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]|\[\*\]", p):
        if cur is None:
            return None
        if part.startswith("[") and part != "[*]":
            idx = int(part[1:-1])
            cur = cur[idx] if isinstance(cur, list) and -len(cur) <= idx < len(cur) else None
        elif part == "[*]":
            continue
        elif isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            cur = [x.get(part) for x in cur if isinstance(x, dict)]
        else:
            return None
    return cur


def _json_filter_rows(rows: list, sel: str, ctx: dict) -> list:
    m = re.search(r":(has|not)\((\w+):contains\((.*?)\)\)$", sel or "")
    if not m or not isinstance(rows, list):
        return rows
    kind, key, val = m.group(1), m.group(2), m.group(3).strip("'\"")
    if not val:
        return rows
    def hit(r):
        return isinstance(r, dict) and val.lower() in str(r.get(key, "")).lower()
    return [r for r in rows if hit(r) == (kind == "has")]


# ------------------------------------------------------------------- filters --
def _args(f: dict) -> list:
    a = f.get("args")
    if a is None:
        return []
    return a if isinstance(a, list) else [a]


def apply_filters(value: Any, filters: list | None, ctx: dict) -> Any:
    for f in filters or []:
        name = (f.get("name") or "").lower()
        args = [render(a, ctx) if isinstance(a, str) else a for a in _args(f)]
        if isinstance(value, list) and name != "jsonjoinarray":
            value = ", ".join(str(x) for x in value)
        v = "" if value is None else str(value)
        try:
            value = _FILTERS[name](v, *args) if name in _FILTERS else v
        except IndexerError:
            raise
        except Exception as e:
            L.debug("filter %s failed on %r: %s", name, v[:80], e)
            value = v
        if name not in _FILTERS:
            L.debug("unknown filter %s ignored", name)
    return value


def _f_querystring(v, name):
    q = parse_qs(urlparse(urljoin("http://x/", v)).query)
    vals = q.get(str(name))
    return vals[0] if vals else ""


def _f_regexp(v, pattern):
    m = compile_net(str(pattern)).search(v)
    if not m:
        return ""
    return m.group(1) if m.groups() and m.group(1) is not None else m.group(0)


def _f_split(v, sep, idx=0):
    parts = v.split(str(sep))
    i = int(idx)
    try:
        return parts[i]
    except IndexError:
        return ""


def _f_dateparse(v, fmt=""):
    ts = B.parse_date_fmt(v, str(fmt)) if fmt else B.parse_date(v)
    return _iso(ts) if ts else v


def _f_timeago(v, *_):
    ts = B.parse_relative(v) or B.parse_date(v)
    return _iso(ts) if ts else v


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def _f_diacritics(v, *_):
    return "".join(c for c in unicodedata.normalize("NFKD", v) if not unicodedata.combining(c))


def _f_validate(v, allowed=""):
    allow = [a.strip().lower() for a in re.split(r"[,|]", str(allowed)) if a.strip()]
    words = [w.strip() for w in re.split(r"[,|/]", v) if w.strip()]
    return ", ".join(w for w in words if w.lower() in allow)


_FILTERS = {
    "querystring": _f_querystring,
    "regexp": _f_regexp,
    "re_replace": lambda v, p, r="": compile_net(str(p)).sub(net_replacement(str(r)), v),
    "replace": lambda v, a, b="": v.replace(str(a), str(b)),
    "split": _f_split,
    "trim": lambda v, cut=None: v.strip(str(cut)) if cut else v.strip(),
    "prepend": lambda v, s: str(s) + v,
    "append": lambda v, s: v + str(s),
    "tolower": lambda v, *_: v.lower(),
    "toupper": lambda v, *_: v.upper(),
    "urldecode": lambda v, *_: unquote(v.replace("+", " ")),
    "urlencode": lambda v, *_: quote_plus(v),
    "htmldecode": lambda v, *_: _html.unescape(v),
    "htmlencode": lambda v, *_: _html.escape(v),
    "dateparse": _f_dateparse,
    "timeparse": _f_dateparse,
    "timeago": _f_timeago,
    "reltime": _f_timeago,
    "fuzzytime": _f_timeago,
    "diacritics": _f_diacritics,
    "validate": _f_validate,
    "validfilename": lambda v, *_: re.sub(r'[<>:"/\\|?*]', "", v),
    "strdump": lambda v, *_: v,
    "hexdump": lambda v, *_: v,
    "andmatch": lambda v, *_: v,
    "jsonjoinarray": lambda v, path=".", sep=",": v,
}


def jsonjoin(value: Any, f: dict) -> Any:
    a = _args(f)
    sep = str(a[1]) if len(a) > 1 else ","
    if isinstance(value, list):
        return sep.join(str(x) for x in value)
    return value


# ---------------------------------------------------------------- definition --
class Definition:
    def __init__(self, data: dict, source: str = ""):
        self.d = data
        self.source = source

    @property
    def id(self) -> str: return str(self.d.get("id"))

    @property
    def name(self) -> str: return str(self.d.get("name") or self.id)

    @property
    def type(self) -> str: return str(self.d.get("type") or "public")

    @property
    def language(self) -> str: return str(self.d.get("language") or "en-US")

    @property
    def links(self) -> list[str]: return [str(x) for x in self.d.get("links") or []]

    @property
    def caps(self) -> dict: return self.d.get("caps") or {}

    @property
    def login(self) -> dict | None: return self.d.get("login")

    @property
    def search(self) -> dict: return self.d.get("search") or {}

    @property
    def settings(self) -> list[dict]: return self.d.get("settings") or []

    @property
    def request_delay(self) -> float:
        try:
            return float(self.d.get("requestDelay") or 0)
        except (TypeError, ValueError):
            return 0.0

    def category_map(self) -> list[dict]:
        out = []
        # older format: caps.categories: {siteId: "TV/HD"}
        for k, v in (self.caps.get("categories") or {}).items():
            cat = str(v)
            out.append({"id": str(k), "cat": cat, "newznab": B.NEWZNAB_CATS.get(cat), "desc": cat})
        for m in self.caps.get("categorymappings") or []:
            cat = str(m.get("cat") or "")
            out.append({"id": str(m.get("id")), "cat": cat, "newznab": B.NEWZNAB_CATS.get(cat),
                        "desc": str(m.get("desc") or "")})
        return out

    def site_cats(self, kind: str) -> list[str]:
        prefix = "TV" if kind == "tv" else "Movies"
        return [m["id"] for m in self.category_map() if m["cat"].split("/")[0] == prefix]

    def kinds(self) -> list[str]:
        cats = {m["cat"].split("/")[0] for m in self.category_map()}
        return [k for k, p in (("tv", "TV"), ("movies", "Movies")) if p in cats]

    def modes(self) -> dict:
        return self.caps.get("modes") or {}

    def unsupported_reason(self) -> str | None:
        lg = self.login or {}
        if lg.get("captcha") and lg.get("method") != "cookie":
            return "Login needs a captcha, which can't be automated. Use the site's cookie instead if offered."
        if not self.kinds():
            return "This site has no TV or movie categories."
        return None

    def summary(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": str(self.d.get("description") or ""),
            "type": self.type, "language": self.language, "links": self.links, "kinds": self.kinds(),
            "needs_login": bool(self.login), "login_method": (self.login or {}).get("method"),
            "flaresolverr_hint": any(s.get("type") == "info_flaresolverr" for s in self.settings),
            "unsupported": self.unsupported_reason(),
        }

    def settings_schema(self) -> list[dict]:
        out = [{"name": "base_url", "type": "select" if len(self.links) > 1 else "text", "label": "Site address",
                "default": self.links[0] if self.links else "",
                "options": {l: l for l in self.links} if len(self.links) > 1 else None}]
        for s in self.settings:
            t = s.get("type")
            item = {"name": s.get("name"), "type": t, "label": s.get("label") or s.get("name"),
                    "default": s.get("default")}
            if t in ("select", "multi-select"):
                item["options"] = {str(k): str(v) for k, v in (s.get("options") or {}).items()}
            if t and t.startswith("info"):
                item["text"] = s.get("default") or ""
            out.append(item)
        return out


def load_definition(path) -> Definition:
    import yaml
    with open(path, encoding="utf-8") as fh:
        return Definition(yaml.safe_load(fh), str(path))


# -------------------------------------------------------------------- engine --
class CardigannIndexer:
    def __init__(self, indexer_id: int, name: str, definition: Definition, cfg: dict, session: Session,
                 priority: int = 25):
        self.id = indexer_id
        self.name = name
        self.defn = definition
        self.cfg = cfg
        self.session = session
        self.priority = priority
        self.session.min_interval = max(self.session.min_interval, definition.request_delay)
        enc = definition.d.get("encoding")
        if enc and enc.lower() not in ("utf-8", "utf8"):
            self.session.encoding = enc
        self.logged_in = bool(session.cookies) and bool(definition.login)

    # ---- context
    @property
    def base_url(self) -> str:
        url = self.cfg.get("base_url") or (self.defn.links[0] if self.defn.links else "")
        return url if url.endswith("/") else url + "/"

    def config_vars(self) -> dict:
        cfg: dict[str, Any] = {"sitelink": self.base_url}
        for s in self.defn.settings:
            name = s.get("name")
            if not name:
                continue
            t = s.get("type")
            v = self.cfg.get(name, s.get("default"))
            if t == "checkbox":
                v = "True" if (v is True or str(v).lower() in ("true", "1", "on", "yes")) else None
            elif t == "multi-select":
                v = v if isinstance(v, list) else ([v] if v not in (None, "") else [])
            elif v is None:
                v = ""
            cfg[name] = v
        return cfg

    def _ctx(self, query: dict | None = None, result: dict | None = None) -> dict:
        q = query or {}
        return {
            "Config": self.config_vars(),
            "Keywords": q.get("keywords", ""),
            "Query": q.get("query", {}),
            "Categories": q.get("categories", []),
            "True": "True", "False": None,
            "Today": {"Year": dt.date.today().year},
            "Result": result or {},
        }

    def url(self, path: str, ctx: dict) -> str:
        p = render(path, ctx).strip()
        if re.match(r"^[a-z][a-z0-9+.-]*:", p, re.I):
            return p
        return urljoin(self.base_url, p)

    def _headers(self, hdefs: dict | None, ctx: dict) -> dict:
        out = {}
        for k, v in (hdefs or {}).items():
            vals = v if isinstance(v, list) else [v]
            out[k] = ", ".join(render(str(x), ctx) for x in vals)
        return out

    # ---- login
    async def ensure_login(self, force: bool = False) -> None:
        lg = self.defn.login
        if not lg:
            return
        if self.logged_in and not force:
            return
        await self.login()

    async def login(self) -> None:
        lg = self.defn.login or {}
        method = (lg.get("method") or "post").lower()
        ctx = self._ctx()
        if lg.get("captcha") and method != "cookie":
            raise IndexerError("captcha", "This site's login needs a captcha, which can't be automated.")
        for item in lg.get("cookies") or []:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                self.session.cookies[k.strip()] = v.strip()
        hdrs = self._headers(lg.get("headers"), ctx)
        if method == "cookie":
            raw = str(ctx["Config"].get("cookie") or "")
            if not raw:
                raise IndexerError("auth", "Paste the site's cookie into this indexer's settings.")
            for part in raw.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    self.session.cookies[k.strip()] = v.strip()
        elif method in ("post", "get", "oneurl"):
            inputs = {k: render(str(v), ctx) for k, v in (lg.get("inputs") or {}).items()}
            url = self.url(lg.get("path") or "", ctx)
            if method == "post":
                r = await self.session.request("POST", url, data=inputs, headers=hdrs)
            else:
                r = await self.session.request("GET", url, params=inputs or None, headers=hdrs)
            self._check_login_errors(r, lg)
        elif method == "form":
            form_url = self.url(lg.get("path") or "", ctx)
            page = await self.session.request("GET", form_url, headers=hdrs)
            soup = BeautifulSoup(page.text, "lxml")
            form = select_one(soup, render(lg.get("form") or "form", ctx))
            if form is None:
                raise IndexerError("auth", "Couldn't find the login form on the site.")
            data: dict[str, str] = {}
            for inp in form.select("input[name]"):
                t = (inp.get("type") or "").lower()
                if t in ("checkbox", "radio") and not inp.has_attr("checked"):
                    continue
                if t in ("submit", "image", "button") and inp.get("name") not in (lg.get("inputs") or {}):
                    continue
                data[inp["name"]] = inp.get("value") or ""
            for sel in form.select("select[name]"):
                opt = sel.select_one("option[selected]") or sel.select_one("option")
                data[sel["name"]] = (opt.get("value") or opt.get_text()) if opt else ""
            for ta in form.select("textarea[name]"):
                data[ta["name"]] = ta.get_text()
            for k, v in (lg.get("inputs") or {}).items():
                data[k] = render(str(v), ctx)
            for k, sdef in (lg.get("selectorinputs") or {}).items():
                el = select_one(soup, render(sdef.get("selector", ""), ctx))
                if el is not None:
                    val = el.get(sdef["attribute"]) if sdef.get("attribute") else text_of(el)
                    data[k] = apply_filters(val or "", sdef.get("filters"), ctx)
            action = form.get("action") or form_url
            if lg.get("submitpath"):
                action = self.url(lg["submitpath"], ctx)
            action = urljoin(page.url, action)
            fmethod = (form.get("method") or "post").upper()
            if fmethod == "GET":
                r = await self.session.request("GET", action, params=data, headers=hdrs)
            else:
                r = await self.session.request("POST", action, data=data, headers={**hdrs, "Referer": page.url})
            self._check_login_errors(r, lg)
        else:
            raise IndexerError("unsupported", f"Login method '{method}' isn't supported.")

        test = lg.get("test") or {}
        if test.get("path") or test.get("selector"):
            tr = await self.session.request("GET", self.url(test.get("path") or "", ctx), headers=hdrs)
            if test.get("selector"):
                soup = BeautifulSoup(tr.text, "lxml")
                if select_one(soup, render(test["selector"], ctx)) is None:
                    if method == "cookie":
                        raise IndexerError("auth", "The cookie didn't log in — it may have expired. Copy a fresh one "
                                                   "from your browser.")
                    raise IndexerError("auth", "Login failed — check your username and password.")
        self.logged_in = True

    def _check_login_errors(self, r: Resp, lg: dict) -> None:
        errs = lg.get("error") or []
        if not errs:
            return
        soup = BeautifulSoup(r.text, "lxml")
        for e in errs:
            el = select_one(soup, render(e.get("selector", ""), self._ctx()))
            if el is not None:
                msg = ""
                mdef = e.get("message")
                if isinstance(mdef, dict):
                    if mdef.get("text"):
                        msg = render(str(mdef["text"]), self._ctx())
                    elif mdef.get("selector"):
                        msg = text_of(select_one(soup, mdef["selector"]))
                msg = msg or text_of(el)
                raise IndexerError("auth", f"Login failed: {msg[:200] or 'the site reported an error'}")

    # ---- search
    def build_query(self, kind: str, keywords: str, season: int | None = None, ep: int | None = None,
                    imdb_id: str | None = None, tvdb_id: int | None = None, tmdb_id: int | None = None,
                    year: int | None = None) -> dict:
        modes = self.defn.modes()
        if kind == "tv":
            mode = "tv-search" if "tv-search" in modes else "search"
            qtype = "tvsearch"
        elif kind == "movies":
            mode = "movie-search" if "movie-search" in modes else "search"
            qtype = "movie"
        else:
            mode, qtype = "search", "search"
        params = set(modes.get(mode) or ["q"])
        q = {"Type": qtype, "Q": keywords, "Keywords": keywords, "Year": year or "",
             "Season": season if season is not None and "season" in params else "",
             "Ep": ep if ep is not None and "ep" in params else "",
             "IMDBID": imdb_id if imdb_id and "imdbid" in params else "",
             "IMDBIDShort": (imdb_id or "").replace("tt", "") if imdb_id and "imdbid" in params else "",
             "TVDBID": tvdb_id if tvdb_id and "tvdbid" in params else "",
             "TMDBID": tmdb_id if tmdb_id and "tmdbid" in params else "",
             "TVMazeID": "", "TraktID": "", "DoubanID": "", "Genre": "", "Limit": 100, "Offset": 0,
             "Extended": 1, "Artist": "", "Album": "", "Author": "", "Title": "", "Episode": ""}
        cats = self.defn.site_cats(kind) if kind in ("tv", "movies") else []
        if not cats and kind in ("tv", "movies"):
            cats = []
        q["Categories"] = cats
        return {"keywords": keywords, "query": q, "categories": cats, "kind": kind}

    def _apply_keyword_filters(self, kw: str, ctx: dict) -> str:
        return str(apply_filters(kw, self.defn.search.get("keywordsfilters"), ctx) or "")

    async def search(self, query: dict) -> list[B.Release]:
        await self.ensure_login()
        s = self.defn.search
        ctx = self._ctx(query)
        kw = self._apply_keyword_filters(query.get("keywords", ""), ctx)
        ctx["Keywords"] = kw
        ctx["Query"] = {**ctx["Query"], "Keywords": kw}
        paths = s.get("paths") or ([{"path": s["path"]}] if s.get("path") else [])
        results: list[B.Release] = []
        seen: set = set()
        cats = set(query.get("categories") or [])
        for pdef in paths:
            pcats = [str(c) for c in pdef.get("categories") or []]
            if pcats and cats and not (cats & set(pcats)):
                continue
            for attempt in (1, 2):
                try:
                    rows = await self._run_path(pdef, ctx, query)
                    break
                except _LoggedOut:
                    if attempt == 2 or not self.defn.login:
                        raise IndexerError("auth", "The site logged us out and logging back in didn't work.")
                    await self.ensure_login(force=True)
            for r in rows:
                key = (r.download_url or r.magnet or r.info_hash or r.guid, r.title)
                if key in seen:
                    continue
                seen.add(key)
                results.append(r)
        return results

    async def _run_path(self, pdef: dict, ctx: dict, query: dict) -> list[B.Release]:
        s = self.defn.search
        url = self.url(pdef.get("path") or "", ctx)
        method = (pdef.get("method") or "get").upper()
        inputs: dict[str, str] = {}
        raw_parts: list[str] = []
        allow_empty = bool(s.get("allowEmptyInputs"))
        src_inputs = dict(s.get("inputs") or {}) if pdef.get("inheritinputs", True) else {}
        src_inputs.update(pdef.get("inputs") or {})
        for k, v in src_inputs.items():
            val = render(str(v), ctx) if v is not None else ""
            if k == "$raw":
                if val:
                    raw_parts.append(val.strip("&"))
                continue
            if val == "" and not allow_empty:
                continue
            inputs[k] = val
        hdrs = self._headers(s.get("headers"), ctx)
        if method == "POST":
            body = urlencode(inputs)
            if raw_parts:
                body = "&".join([p for p in [body] + raw_parts if p])
            r = await self.session.request("POST", url, raw_body=body, headers=hdrs,
                                           follow=pdef.get("followredirect", self.defn.d.get("followredirect", True)))
        else:
            qs = urlencode(inputs)
            if raw_parts:
                qs = "&".join([p for p in [qs] + raw_parts if p])
            full = url + (("&" if "?" in url else "?") + qs if qs else "")
            r = await self.session.request("GET", full, headers=hdrs)
        if r.status >= 400:
            if r.status in (401, 403) and self.defn.login:
                raise _LoggedOut()
            raise IndexerError("http", http_error_message(r))
        resp_def = pdef.get("response") or {}
        rtype = (resp_def.get("type") or "html").lower()
        text = r.text
        if resp_def.get("noResultsMessage") and text.strip() == str(resp_def["noResultsMessage"]).strip():
            return []
        if rtype == "json":
            return self._parse_json(text, ctx, query)
        if rtype == "xml":
            return self._parse_html(text, ctx, query, xml=True)
        if self.defn.login and self._looks_logged_out(text):
            raise _LoggedOut()
        self._check_search_errors(text)
        return self._parse_html(text, ctx, query)

    def _looks_logged_out(self, text: str) -> bool:
        lg = self.defn.login or {}
        if lg.get("method") == "cookie":
            return False
        soup = BeautifulSoup(text[:200000], "lxml")
        form_sel = lg.get("form") or "form"
        if soup.select_one("input[type=password]") and soup.select_one(css(form_sel)) is not None:
            return not soup.select(css(self.defn.search.get("rows", {}).get("selector", "x-none")))
        return False

    def _check_search_errors(self, text: str) -> None:
        errs = self.defn.search.get("error") or []
        if not errs:
            return
        soup = BeautifulSoup(text, "lxml")
        for e in errs:
            el = select_one(soup, render(e.get("selector", ""), self._ctx()))
            if el is not None:
                msg = e.get("message")
                m = text_of(select_one(soup, msg["selector"])) if isinstance(msg, dict) and msg.get("selector") \
                    else text_of(el)
                raise IndexerError("http", f"The site reported: {m[:200]}")

    # ---- row parsing
    def _parse_html(self, text: str, ctx: dict, query: dict, xml: bool = False) -> list[B.Release]:
        s = self.defn.search
        soup = BeautifulSoup(text, "xml" if xml else "lxml")
        rows_def = s.get("rows") or {}
        rows = select_all(soup, render(rows_def.get("selector", ""), ctx))
        after = int(rows_def.get("after") or 0)
        if after:
            merged, i = [], 0
            while i < len(rows):
                wrapper = soup.new_tag("tr")
                for r in rows[i:i + 1 + after]:
                    for child in list(r.children):
                        wrapper.append(copy.copy(child))
                merged.append(wrapper)
                i += 1 + after
            rows = merged
        out = []
        for row in rows:
            rel = self._row_to_release(row, ctx, json_mode=False)
            if rel:
                out.append(rel)
        return self._post_filter(out, rows_def, ctx)

    def _parse_json(self, text: str, ctx: dict, query: dict) -> list[B.Release]:
        s = self.defn.search
        t = text.strip()
        if t.startswith("<"):   # JSON rendered inside a browser page (FlareSolverr)
            pre = BeautifulSoup(t, "lxml").find("pre")
            t = pre.get_text() if pre else BeautifulSoup(t, "lxml").get_text()
        try:
            data = json.loads(t) if t.strip() else None
        except ValueError as e:
            raise IndexerError("parse", "The site returned something that isn't valid JSON.") from e
        if data is None:
            return []
        rows_def = s.get("rows") or {}
        sel = render(rows_def.get("selector", ""), ctx)
        rows = json_select(data, sel)
        rows = _json_filter_rows(rows, sel, ctx)
        if rows is None:
            if rows_def.get("missingAttributeEqualsNoResults"):
                return []
            rows = []
        if isinstance(rows, dict):
            rows = list(rows.values()) if all(isinstance(v, dict) for v in rows.values()) else [rows]
        expanded: list[tuple[Any, Any]] = []
        attr = rows_def.get("attribute")
        for r in rows:
            if attr:
                sub = json_select(r, attr)
                if sub is None and rows_def.get("missingAttributeEqualsNoResults"):
                    continue
                for x in sub if isinstance(sub, list) else ([sub] if sub else []):
                    expanded.append((x, r))
            else:
                expanded.append((r, None))
        out = []
        for row, parent in expanded:
            rel = self._row_to_release(row, ctx, json_mode=True, parent=parent)
            if rel:
                out.append(rel)
        return self._post_filter(out, rows_def, ctx)

    def _post_filter(self, rels: list[B.Release], rows_def: dict, ctx: dict) -> list[B.Release]:
        for f in rows_def.get("filters") or []:
            if (f.get("name") or "").lower() == "andmatch":
                kw = str(ctx.get("Keywords") or "")
                words = [w for w in re.split(r"[\s._-]+", kw.lower()) if w and len(w) > 0]
                if words:
                    rels = [r for r in rels if all(w in r.title.lower() for w in words)]
        return rels

    def _field_value(self, row, fdef: Any, ctx: dict, json_mode: bool, parent: Any) -> tuple[bool, Any]:
        """Returns (found, value)."""
        if not isinstance(fdef, dict):
            return True, render(str(fdef), ctx)
        if "text" in fdef and "selector" not in fdef:
            val: Any = render(str(fdef["text"]) if fdef["text"] is not None else "", ctx)
            return True, self._filters(val, fdef, ctx)
        found = True
        val = None
        if json_mode:
            sel = render(str(fdef.get("selector", "")), ctx) if "selector" in fdef else ""
            val = json_select(row, sel, parent) if sel else None
            if "case" in fdef:
                key = "" if val is None else str(val)
                cases = fdef["case"] or {}
                val = cases.get(key, cases.get(val, cases.get("*")))
                if isinstance(val, str):
                    val = render(val, ctx)
            if val is None:
                found = False
            elif isinstance(val, bool):
                val = "True" if val else "False"
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                val = val
        else:
            el = row
            if fdef.get("selector"):
                sel = render(str(fdef["selector"]), ctx)
                el = select_one(row, sel)
                if el is None:
                    found = False
            if found:
                if fdef.get("remove"):
                    el = copy.copy(el)
                    for r_ in select_all(el, render(str(fdef["remove"]), ctx)):
                        r_.decompose()
                if "case" in fdef:
                    val = None
                    for csel, cval in (fdef.get("case") or {}).items():
                        if csel == "*":
                            continue
                        try:
                            rs = render(str(csel), ctx)
                            hit = el.select_one(css(rs)) is not None or _self_matches(el, rs)
                        except Exception:
                            hit = False
                        if hit:
                            val = render(str(cval), ctx)
                            break
                    if val is None and "*" in (fdef.get("case") or {}):
                        val = render(str(fdef["case"]["*"]), ctx)
                    if val is None:
                        found = False
                elif fdef.get("attribute"):
                    a = el.get(fdef["attribute"])
                    val = " ".join(a) if isinstance(a, list) else a
                    if val is None:
                        found = False
                else:
                    val = text_of(el)
        if found:
            val = self._filters(val, fdef, ctx)
        return found, val

    def _filters(self, val, fdef, ctx):
        fl = fdef.get("filters") or []
        for f in fl:
            if (f.get("name") or "").lower() == "jsonjoinarray":
                val = jsonjoin(val, f)
        rest = [f for f in fl if (f.get("name") or "").lower() != "jsonjoinarray"]
        return apply_filters(val, rest, ctx) if rest else val

    def _row_to_release(self, row, ctx: dict, json_mode: bool, parent: Any = None) -> B.Release | None:
        fields = self.defn.search.get("fields") or {}
        result: dict[str, Any] = {}
        rctx = dict(ctx)
        rctx["Result"] = result
        for raw_name, fdef in fields.items():
            name, _, modifier = str(raw_name).partition("|")
            try:
                found, val = self._field_value(row, fdef, rctx, json_mode, parent)
            except IndexerError:
                raise
            except Exception as e:
                L.debug("%s: field %s failed: %s", self.name, name, e)
                found, val = False, None
            optional = isinstance(fdef, dict) and (fdef.get("optional") or "default" in fdef)
            if (not found or val in (None, "")) and isinstance(fdef, dict) and "default" in fdef:
                val = render(str(fdef["default"]) if fdef["default"] is not None else "", rctx)
                found = True
            if not found:
                if optional or name.startswith("_") or name not in ("title", "download"):
                    result.setdefault(name, None)
                    continue
                return None   # required field missing → skip row
            if modifier == "append" and result.get(name):
                result[name] = f"{result[name]}{val}"
            else:
                result[name] = val
        return self._to_release(result)

    def _to_release(self, r: dict) -> B.Release | None:
        title = _html.unescape(str(r.get("title") or "")).strip()
        if not title:
            return None
        def absu(u):
            if not u:
                return None
            u = str(u).strip()
            if u.startswith("magnet:"):
                return u
            return urljoin(self.base_url, u)
        download = absu(r.get("download"))
        magnet = absu(r.get("magnet")) if r.get("magnet") else None
        if download and download.startswith("magnet:"):
            magnet, download = download, None
        info_hash = (str(r.get("infohash") or "").strip().lower() or None)
        if info_hash and not re.fullmatch(r"[0-9a-f]{40}", info_hash):
            info_hash = None
        if not magnet and info_hash and not download:
            from ..torrent import build_magnet
            magnet = build_magnet(info_hash, title)
        if not download and not magnet:
            return None
        details = absu(r.get("details")) or download or magnet
        cats: list = []
        cmap = {m["id"]: m for m in self.defn.category_map()}
        desc_map = {m["desc"]: m for m in self.defn.category_map() if m["desc"]}
        raw_cat = r.get("category")
        for c in re.split(r"[,\s]+", str(raw_cat)) if raw_cat not in (None, "") else []:
            m = cmap.get(c)
            if m and m["newznab"]:
                cats.append(m["newznab"])
        if r.get("categorydesc"):
            m = desc_map.get(str(r["categorydesc"]).strip())
            if m and m["newznab"]:
                cats.append(m["newznab"])
        pub = B.parse_date(r.get("date")) if r.get("date") else None
        mst = B.parse_float(r.get("minimumseedtime"))
        return B.Release(
            title=title, indexer_id=self.id, indexer_name=self.name,
            guid=str(download or magnet or r.get("details")),
            download_url=download, magnet=magnet, info_hash=info_hash, details_url=details,
            size=B.parse_size(r.get("size")), seeders=B.parse_int(r.get("seeders")),
            leechers=B.parse_int(r.get("leechers")), grabs=B.parse_int(r.get("grabs")),
            files=B.parse_int(r.get("files")), published=pub or time.time(), categories=sorted(set(cats)),
            imdb_id=B.normalize_imdb(r.get("imdbid") or r.get("imdb")),
            tvdb_id=B.parse_int(r.get("tvdbid")), tmdb_id=B.parse_int(r.get("tmdbid")),
            min_ratio=B.parse_float(r.get("minimumratio")), min_seed_time=int(mst) if mst else None,
            download_factor=B.parse_float(r.get("downloadvolumefactor")), indexer_priority=self.priority,
        )

    # ---- download
    async def fetch_torrent(self, rel: B.Release) -> tuple[bytes | None, str | None]:
        """Returns (torrent bytes, magnet) — one of them set."""
        if rel.magnet and not rel.download_url:
            return None, rel.magnet
        await self.ensure_login()
        dl = self.defn.d.get("download") or {}
        ctx = self._ctx()
        url = rel.download_url or rel.details_url
        hdrs = self._headers(dl.get("headers"), ctx)
        if dl.get("before"):
            b = dl["before"]
            bctx = self._ctx(result={"_details": rel.details_url})
            bpath = self.url(b.get("path") or "", bctx) if b.get("path") else url
            inputs = {k: render(str(v), bctx) for k, v in (b.get("inputs") or {}).items()}
            if (b.get("method") or "get").lower() == "post":
                await self.session.request("POST", bpath, data=inputs, headers=hdrs)
            else:
                await self.session.request("GET", bpath, params=inputs or None, headers=hdrs)
        if dl.get("infohash"):
            page = await self.session.request("GET", url, headers=hdrs)
            soup = BeautifulSoup(page.text, "lxml")
            ih = dl["infohash"]
            h_el = select_one(soup, render((ih.get("hash") or {}).get("selector", ""), ctx))
            t_el = select_one(soup, render((ih.get("title") or {}).get("selector", ""), ctx))
            if h_el is not None:
                hv = h_el.get(ih["hash"]["attribute"]) if ih["hash"].get("attribute") else text_of(h_el)
                hv = apply_filters(hv, ih["hash"].get("filters"), ctx)
                tv = text_of(t_el) if t_el is not None else rel.title
                from ..torrent import build_magnet
                return None, build_magnet(str(hv).lower(), str(tv))
        if dl.get("selectors"):
            page = await self.session.request((dl.get("method") or "get").upper(), url, headers=hdrs)
            if page.redirect_to:
                return None, page.redirect_to
            soup = BeautifulSoup(page.text, "lxml")
            target = None
            for sdef in dl["selectors"]:
                el = select_one(soup, render(sdef.get("selector", ""), ctx))
                if el is None:
                    continue
                v = el.get(sdef["attribute"]) if sdef.get("attribute") else text_of(el)
                v = apply_filters(v, sdef.get("filters"), ctx)
                if v:
                    target = str(v)
                    break
            if not target:
                raise IndexerError("parse", "Couldn't find the download link on the release page.")
            if target.startswith("magnet:"):
                return None, target
            url = urljoin(page.url, target)
        r = await self.session.request("GET", url, headers={**hdrs, "Accept": "application/x-bittorrent,*/*"})
        if r.redirect_to:
            return None, r.redirect_to
        if r.status >= 400:
            raise IndexerError("http", f"Download failed: HTTP {r.status}")
        from ..torrent import is_torrent
        if is_torrent(r.content):
            return r.content, None
        if rel.magnet:
            return None, rel.magnet
        txt = r.content[:2000].decode("utf-8", "ignore").strip()
        if txt.startswith("magnet:"):
            return None, txt.split()[0]
        raise IndexerError("http", "The site didn't return a torrent file (you may need to log in again).")

    async def test(self) -> int:
        await self.ensure_login(force=bool(self.defn.login))
        q = self.build_query("tv" if "tv" in self.defn.kinds() else "movies", "")
        rels = await self.search(q)
        return len(rels)


def http_error_message(r: Resp) -> str:
    """'The site answered HTTP 400 for /search/... — <title or first words of the page>'."""
    path = urlparse(r.url).path or "/"
    if len(path) > 60:
        path = path[:57] + "..."
    text = r.text[:20000]
    snippet = ""
    try:
        soup = BeautifulSoup(text, "lxml")
        snippet = (soup.title.get_text(" ", strip=True) if soup.title else "") or soup.get_text(" ", strip=True)
    except Exception:
        snippet = text
    snippet = re.sub(r"\s+", " ", snippet).strip()[:140]
    L.warning("HTTP %s from %s — %s", r.status, r.url, snippet)
    return f"The site answered HTTP {r.status} for {path}" + (f" — \"{snippet}\"" if snippet else "") + "."


def _self_matches(el, sel: str) -> bool:
    try:
        parent = el.parent
        return parent is not None and el in parent.select(css(sel))
    except Exception:
        return False


class _LoggedOut(Exception):
    pass
