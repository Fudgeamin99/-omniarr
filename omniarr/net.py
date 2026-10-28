"""HTTP layer for talking to indexers: politeness, Cloudflare (FlareSolverr), proxies, ISP blocks."""
from __future__ import annotations

import asyncio
import re
import ssl
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urljoin

import httpx2 as httpx

from . import config, log, settings

L = log.get("net")


class IndexerError(Exception):
    """kind: cloudflare | isp_block | auth | http | parse | unsupported | limit | network | captcha"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class Resp:
    status: int
    url: str
    headers: dict
    content: bytes
    encoding: str | None = None
    redirect_to: str | None = None   # set when a redirect pointed at a magnet: link

    @property
    def text(self) -> str:
        enc = self.encoding
        if not enc:
            ct = self.headers.get("content-type", "")
            m = re.search(r"charset=([\w-]+)", ct)
            enc = m.group(1) if m else "utf-8"
        try:
            return self.content.decode(enc, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


_clients: dict[str, httpx.AsyncClient] = {}


_ssl_ctx: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """System certificates plus the up-to-date Mozilla bundle (NAS images can ship an old store)."""
    global _ssl_ctx
    if _ssl_ctx is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass
        _ssl_ctx = ctx
    return _ssl_ctx


def is_cert_error(e: BaseException) -> bool:
    text = f"{e} {e.__cause__ or ''} {e.__context__ or ''}"
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text.lower()


CERT_BLOCK_MSG = ("Your internet provider is blocking this site (its security certificate was replaced). "
                  "Omniarr routes blocked sites through the proxy automatically once one is set — the docker-compose "
                  "file includes a free one (Cloudflare WARP, address http://warp:1080). Set it in Connections → "
                  "Staying unblocked → Proxy.")


def client(proxy: str | None = None, timeout: float = 30.0) -> httpx.AsyncClient:
    key = f"{proxy or ''}|{timeout}"
    c = _clients.get(key)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(proxy=proxy or None, timeout=httpx.Timeout(timeout, connect=15.0),
                              follow_redirects=False, trust_env=not proxy, verify=ssl_context(),
                              limits=httpx.Limits(max_connections=50, max_keepalive_connections=10))
        _clients[key] = c
    return c


async def close_all() -> None:
    for c in list(_clients.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _clients.clear()


# ----------------------------------------------------------------- detection --
_CF_MARKERS = ("cf-chl", "challenge-platform", "cf_chl_opt", "just a moment...", "checking your browser",
               "attention required! | cloudflare", "cf-browser-verification", "ddos-guard",
               "enable javascript and cookies to continue")
_ISP_MARKERS = (
    "this site has been blocked", "this website has been blocked", "access to this website has been blocked",
    "access to this site has been blocked", "blocked by court order", "due to a court order",
    "blocked as a result of a court order", "has been blocked by your internet service provider",
    "this site is blocked", "site blocked by", "the website you are trying to access has been blocked",
    "blocked under the instructions of", "blocked in accordance with", "access denied by court",
)


def looks_like_cloudflare(r: Resp) -> bool:
    if r.status not in (403, 429, 503) and r.status < 500:
        return False
    head = r.content[:20000].decode("utf-8", "ignore").lower()
    server = r.headers.get("server", "").lower()
    if any(m in head for m in _CF_MARKERS):
        return True
    return "cloudflare" in server and r.status in (403, 503) and "<title>" in head and "cloudflare" in head


def looks_like_isp_block(r: Resp) -> bool:
    if r.status == 451:
        return True
    if len(r.content) > 60000:
        return False
    head = r.content[:60000].decode("utf-8", "ignore").lower()
    return any(m in head for m in _ISP_MARKERS)


# --------------------------------------------------------------- politeness --
_locks: dict[Any, asyncio.Lock] = {}
_last: dict[Any, float] = {}


async def wait_turn(key: Any, min_interval: float) -> None:
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        gap = time.monotonic() - _last.get(key, 0.0)
        if gap < min_interval:
            await asyncio.sleep(min_interval - gap)
        _last[key] = time.monotonic()


# -------------------------------------------------------------- flaresolverr --
async def flaresolverr(url: str, method: str = "GET", post_data: str | None = None,
                       cookies: dict | None = None, proxy: str | None = None) -> dict:
    fs = (settings.get("unblock").get("flaresolverr_url") or "").rstrip("/")
    if not fs:
        raise IndexerError("cloudflare", "This site needs the Cloudflare bypass, but no FlareSolverr address is set.")
    payload: dict[str, Any] = {"cmd": "request.post" if method == "POST" else "request.get", "url": url,
                               "maxTimeout": 60000}
    if post_data is not None:
        payload["postData"] = post_data
    if cookies:
        payload["cookies"] = [{"name": k, "value": v} for k, v in cookies.items()]
    if proxy:
        payload["proxy"] = {"url": proxy}
    try:
        r = await client(timeout=90).post(f"{fs}/v1", json=payload)
    except httpx.HTTPError as e:
        raise IndexerError("cloudflare", f"Couldn't reach FlareSolverr at {fs} ({e.__class__.__name__}).") from e
    try:
        data = r.json()
    except ValueError as e:
        raise IndexerError("cloudflare", f"FlareSolverr returned an unexpected reply (HTTP {r.status_code}).") from e
    if data.get("status") != "ok":
        raise IndexerError("cloudflare", f"FlareSolverr couldn't get past the check: {data.get('message', 'unknown')}")
    sol = data.get("solution") or {}
    return {
        "status": int(sol.get("status") or 200),
        "url": sol.get("url") or url,
        "html": sol.get("response") or "",
        "cookies": {c["name"]: c["value"] for c in sol.get("cookies") or [] if "name" in c},
        "user_agent": sol.get("userAgent") or None,
    }


async def flaresolverr_ping(url: str | None = None) -> str:
    fs = (url or settings.get("unblock").get("flaresolverr_url") or "").rstrip("/")
    if not fs:
        raise IndexerError("cloudflare", "No address set")
    r = await client(timeout=15).get(fs + "/")
    data = r.json()
    return data.get("version") or data.get("msg") or "ok"


# ------------------------------------------------------------------ session --
@dataclass
class Session:
    """Cookie jar + settings for one indexer."""
    key: Any
    name: str
    cookies: dict = field(default_factory=dict)
    user_agent: str | None = None
    use_flaresolverr: bool = False
    use_proxy: bool = False
    min_interval: float = 0.0          # site's own requestDelay; the user setting is applied on top
    encoding: str | None = None
    headers: dict = field(default_factory=dict)
    on_flaresolverr_needed: Any = None   # callback(bool) to persist auto-enable
    on_proxy_needed: Any = None          # callback(bool) to persist "route through proxy"
    cf_detected: bool = False
    polite_by_host: bool = True          # space requests per website (Torznab proxies: per indexer)

    def _proxy(self) -> str | None:
        if self.use_proxy:
            p = settings.get("unblock").get("proxy_url") or ""
            return p or None
        return None

    def _absorb(self, r: httpx.Response) -> None:
        for k, v in r.cookies.items():
            self.cookies[k] = v
        # httpx only keeps cookies matching the domain; also parse raw headers to be safe
        for raw in r.headers.get_list("set-cookie"):
            sc = SimpleCookie()
            try:
                sc.load(raw)
            except Exception:
                continue
            for k, morsel in sc.items():
                if morsel.value == "deleted":
                    self.cookies.pop(k, None)
                else:
                    self.cookies[k] = morsel.value

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    async def request(self, method: str, url: str, **kw) -> Resp:
        """Make a request; if the site looks blocked by the internet provider, retry through the proxy
        (when one is set) and remember that this indexer needs it."""
        try:
            return await self._request(method, url, **kw)
        except IndexerError as e:
            if e.kind != "isp_block" or self.use_proxy or kw.get("_cf_retry"):
                raise
            cfg = settings.get("unblock")
            if not cfg.get("proxy_url") or not cfg.get("auto_proxy", True):
                raise
            L.info("%s: looks blocked by the internet provider — retrying through the proxy", self.name)
            self.use_proxy = True
            try:
                resp = await self._request(method, url, **{**kw, "polite": False})
            except IndexerError:
                self.use_proxy = False
                raise
            if self.on_proxy_needed:
                self.on_proxy_needed(True)
            return resp

    async def _request(self, method: str, url: str, *, params: Any = None, data: Any = None,
                      headers: dict | None = None, follow: bool = True, polite: bool = True,
                      raw_body: str | None = None, _cf_retry: bool = False) -> Resp:
        if polite:
            key = ("host", httpx.URL(url).host) if self.polite_by_host else self.key
            await wait_turn(key, max(self.min_interval, float(settings.get("unblock").get("min_interval") or 0)))
        hdrs = {"User-Agent": self.user_agent or config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
                "Accept-Language": "en-US,en;q=0.9"}
        # merge case-insensitively so a definition's "cookie"/"user-agent" never duplicates ours
        extra_cookie = ""
        for src in (self.headers, headers or {}):
            for k, v in src.items():
                if k.lower() == "cookie":
                    extra_cookie = "; ".join(x for x in (extra_cookie, str(v)) if x)
                    continue
                for existing in [x for x in hdrs if x.lower() == k.lower()]:
                    del hdrs[existing]
                hdrs[k] = v
        proxy = self._proxy()
        c = client(proxy)
        current_url, current_method, body, qp = url, method.upper(), data, params
        resp: httpx.Response | None = None
        for _hop in range(10):
            h = dict(hdrs)
            jar = {}
            for part in extra_cookie.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    jar[k.strip()] = v.strip()
            jar.update(self.cookies)          # cookies the site set win over the definition's defaults
            if jar:
                h["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
            try:
                if raw_body is not None and current_method == "POST":
                    h.setdefault("Content-Type", "application/x-www-form-urlencoded")
                    resp = await c.request(current_method, current_url, params=qp, content=raw_body, headers=h)
                else:
                    resp = await c.request(current_method, current_url, params=qp,
                                           data=body if current_method != "GET" else None, headers=h)
            except httpx.ProxyError as e:
                raise IndexerError("network", f"The proxy didn't work: {e}") from e
            except httpx.ConnectError as e:
                msg = str(e)
                if is_cert_error(e):
                    raise IndexerError("isp_block", CERT_BLOCK_MSG) from e
                if "Name or service not known" in msg or "nodename" in msg or "getaddrinfo" in msg:
                    raise IndexerError("isp_block", "The site's address couldn't be found (DNS). It may be down, "
                                                  "or blocked by your internet provider — try the proxy.") from e
                raise IndexerError("network", f"Couldn't connect to the site ({msg or 'connection refused'}).") from e
            except httpx.TimeoutException as e:
                raise IndexerError("network", "The site took too long to respond.") from e
            except httpx.HTTPError as e:
                raise IndexerError("network", f"Network error: {e.__class__.__name__}: {e}") from e
            self._absorb(resp)
            if follow and resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                loc = resp.headers["location"]
                if loc.startswith("magnet:"):
                    return Resp(resp.status_code, str(resp.url), dict(resp.headers), b"", self.encoding,
                                redirect_to=loc)
                current_url = urljoin(str(resp.url), loc)
                qp = None
                if resp.status_code in (301, 302, 303):
                    current_method, body, raw_body = "GET", None, None
                continue
            break
        assert resp is not None
        out = Resp(resp.status_code, str(resp.url), {k.lower(): v for k, v in resp.headers.items()},
                   resp.content, self.encoding)

        if looks_like_cloudflare(out) and not _cf_retry:
            self.cf_detected = True
            out = await self._via_flaresolverr(method, url, params, data, raw_body, headers, out)
        if settings.get("unblock").get("detect_isp_block") and looks_like_isp_block(out):
            raise IndexerError("isp_block", "This site looks blocked by your internet provider. "
                                            "Turn on 'Route through proxy' for this indexer.")
        return out

    async def _via_flaresolverr(self, method, url, params, data, raw_body, headers, original: Resp) -> Resp:
        if not self.use_flaresolverr:
            if self.on_flaresolverr_needed:
                self.on_flaresolverr_needed(True)   # auto-enable for next time
            self.use_flaresolverr = True
        L.info("%s: Cloudflare check detected, solving with FlareSolverr", self.name)
        full = str(httpx.URL(url, params=params)) if params else url
        post = None
        if method.upper() == "POST":
            post = raw_body if raw_body is not None else str(httpx.QueryParams(data or {}))
        sol = await flaresolverr(full, method.upper(), post, self.cookies, self._proxy())
        self.cookies.update(sol["cookies"])
        if sol["user_agent"]:
            self.user_agent = sol["user_agent"]
        # Retry directly with the clearance cookies (keeps JSON/binary responses intact)...
        try:
            again = await self.request(method, url, params=params, data=data, headers=headers, polite=False,
                                       raw_body=raw_body, _cf_retry=True)
            if not looks_like_cloudflare(again):
                return again
        except IndexerError:
            pass
        # ...otherwise use the page FlareSolverr rendered.
        return Resp(sol["status"], sol["url"], {"content-type": "text/html; charset=utf-8"},
                    sol["html"].encode("utf-8"), "utf-8")


# --------------------------------------------------------------- plain APIs --
async def get_json(url: str, *, params: dict | None = None, headers: dict | None = None,
                   timeout: float = 20.0) -> Any:
    h = {"User-Agent": config.APP_USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    r = await client(timeout=timeout).get(url, params=params, headers=h, follow_redirects=True)
    r.raise_for_status()
    return r.json()
