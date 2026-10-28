"""Subtitle providers: OpenSubtitles.com (REST API) and Podnapisi (JSON search)."""
from __future__ import annotations

import io
import time
import zipfile
from dataclasses import dataclass, field

import httpx2 as httpx

from .. import config, log

L = log.get("subtitles")


class ProviderError(Exception):
    pass


@dataclass
class Candidate:
    provider: str
    id: str
    language: str
    releases: list[str] = field(default_factory=list)
    hearing_impaired: bool = False
    forced: bool = False
    hash_match: bool = False
    downloads: int = 0
    extra: dict = field(default_factory=dict)
    score: int = 0


@dataclass
class Query:
    kind: str                    # episode | movie
    title: str
    year: int | None
    season: int | None
    episode: int | None
    imdb_id: str | None          # movie imdb, or the show's imdb for episodes
    tmdb_id: int | None
    file_hash: str | None
    release_name: str
    language: str = "en"


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        from ..net import ssl_context
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True,
                                    verify=ssl_context())
    return _client


# ------------------------------------------------------------ OpenSubtitles --
class OpenSubtitles:
    id = "opensubtitles"
    name = "OpenSubtitles.com"
    _token: tuple[str, float, str] | None = None   # token, expiry, base url

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api_key = (cfg.get("api_key") or "").strip()

    def configured(self) -> bool:
        return bool(self.api_key)

    def _h(self, auth: bool = False) -> dict:
        h = {"Api-Key": self.api_key, "User-Agent": config.APP_USER_AGENT, "Accept": "application/json",
             "Content-Type": "application/json"}
        if auth and OpenSubtitles._token:
            h["Authorization"] = f"Bearer {OpenSubtitles._token[0]}"
        return h

    def _base(self) -> str:
        if OpenSubtitles._token and OpenSubtitles._token[2]:
            return OpenSubtitles._token[2]
        return config.OPENSUBTITLES_URL

    async def login(self) -> None:
        if not (self.cfg.get("username") and self.cfg.get("password")):
            return
        if OpenSubtitles._token and OpenSubtitles._token[1] > time.time():
            return
        r = await client().post(f"{config.OPENSUBTITLES_URL}/login", headers=self._h(),
                                json={"username": self.cfg["username"], "password": self.cfg["password"]})
        if r.status_code == 401:
            raise ProviderError("OpenSubtitles rejected the username or password.")
        if r.status_code == 403:
            raise ProviderError("OpenSubtitles rejected the API key.")
        r.raise_for_status()
        data = r.json()
        base = data.get("base_url")
        base_url = f"https://{base}/api/v1" if base and "://" not in base and "opensubtitles" in base else \
            config.OPENSUBTITLES_URL
        OpenSubtitles._token = (data.get("token"), time.time() + 23 * 3600, base_url)

    async def test(self) -> str:
        if not self.configured():
            raise ProviderError("Add your OpenSubtitles API key.")
        await self.login()
        r = await client().get(f"{self._base()}/infos/user" if OpenSubtitles._token else
                               f"{config.OPENSUBTITLES_URL}/infos/formats", headers=self._h(auth=True))
        if r.status_code in (401, 403):
            raise ProviderError("OpenSubtitles rejected the API key.")
        r.raise_for_status()
        info = r.json().get("data") or {}
        if isinstance(info, dict) and "allowed_downloads" in info:
            return f"Signed in · {info.get('remaining_downloads', info['allowed_downloads'])} downloads left today"
        return "API key works"

    async def search(self, q: Query) -> list[Candidate]:
        if not self.configured():
            return []
        await self.login()
        params: dict = {"languages": q.language}
        if q.file_hash:
            params["moviehash"] = q.file_hash
        if q.kind == "episode":
            params["type"] = "episode"
            if q.imdb_id:
                params["parent_imdb_id"] = q.imdb_id.replace("tt", "").lstrip("0")
            else:
                params["query"] = q.title
            params["season_number"] = q.season
            params["episode_number"] = q.episode
        else:
            params["type"] = "movie"
            if q.imdb_id:
                params["imdb_id"] = q.imdb_id.replace("tt", "").lstrip("0")
            elif q.tmdb_id:
                params["tmdb_id"] = q.tmdb_id
            else:
                params["query"] = q.title
                if q.year:
                    params["year"] = q.year
        r = await client().get(f"{self._base()}/subtitles", params=params, headers=self._h(auth=True))
        if r.status_code == 429:
            raise ProviderError("OpenSubtitles rate limit — will retry later.")
        if r.status_code in (401, 403):
            raise ProviderError("OpenSubtitles rejected the API key.")
        r.raise_for_status()
        out = []
        for item in r.json().get("data") or []:
            a = item.get("attributes") or {}
            if a.get("ai_translated") or a.get("machine_translated"):
                continue
            files = a.get("files") or []
            if not files:
                continue
            out.append(Candidate(
                provider=self.id, id=str(files[0].get("file_id")), language=a.get("language") or q.language,
                releases=[x for x in [a.get("release")] + [f.get("file_name") for f in files] if x],
                hearing_impaired=bool(a.get("hearing_impaired")), forced=bool(a.get("foreign_parts_only")),
                hash_match=bool(a.get("moviehash_match")), downloads=int(a.get("download_count") or 0)))
        return out

    async def download(self, c: Candidate) -> tuple[bytes, str]:
        await self.login()
        r = await client().post(f"{self._base()}/download", headers=self._h(auth=True),
                                json={"file_id": int(c.id), "sub_format": "srt"})
        if r.status_code == 406 or (r.status_code == 429):
            raise ProviderError("OpenSubtitles daily download limit reached — will retry tomorrow.")
        r.raise_for_status()
        link = r.json().get("link")
        if not link:
            raise ProviderError("OpenSubtitles didn't return a download link.")
        f = await client().get(link)
        f.raise_for_status()
        return f.content, ".srt"


# ----------------------------------------------------------------- Podnapisi --
class Podnapisi:
    id = "podnapisi"
    name = "Podnapisi"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def configured(self) -> bool:
        return True

    async def test(self) -> str:
        r = await client().get(f"{config.PODNAPISI_URL}/subtitles/search/advanced",
                               params={"keywords": "test", "language": "en"},
                               headers={"Accept": "application/json", "User-Agent": config.APP_USER_AGENT})
        r.raise_for_status()
        r.json()
        return "Ready"

    async def search(self, q: Query) -> list[Candidate]:
        params: dict = {"keywords": q.title, "language": q.language}
        if q.year:
            params["year"] = q.year
        if q.kind == "episode":
            params["movie_type"] = "tv-series"
            params["seasons"] = q.season
            params["episodes"] = q.episode
        else:
            params["movie_type"] = "movie"
        r = await client().get(f"{config.PODNAPISI_URL}/subtitles/search/advanced", params=params,
                               headers={"Accept": "application/json", "User-Agent": config.APP_USER_AGENT})
        if r.status_code == 429:
            raise ProviderError("Podnapisi rate limit — will retry later.")
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError as e:
            raise ProviderError("Podnapisi returned an unexpected page.") from e
        out = []
        for item in data.get("data") or []:
            lang = (item.get("language") or q.language).lower()
            if lang[:2] != q.language:
                continue
            flags = item.get("flags") or []
            ep = (item.get("movie") or {}).get("episode_info") or {}
            if q.kind == "episode" and ep:
                if (ep.get("season") not in (None, q.season)) or (ep.get("episode") not in (None, q.episode)):
                    continue
            rels = item.get("custom_releases") or item.get("releases") or []
            out.append(Candidate(
                provider=self.id, id=str(item.get("id")), language=lang[:2], releases=[str(x) for x in rels],
                hearing_impaired="hearing_impaired" in flags, forced="foreign_only" in flags,
                downloads=int((item.get("stats") or {}).get("downloads") or 0)))
        return out

    async def download(self, c: Candidate) -> tuple[bytes, str]:
        r = await client().get(f"{config.PODNAPISI_URL}/subtitles/{c.id}/download",
                               headers={"User-Agent": config.APP_USER_AGENT})
        r.raise_for_status()
        data = r.content
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith((".srt", ".ass", ".ssa", ".sub"))]
                if not names:
                    raise ProviderError("The Podnapisi download had no subtitle file in it.")
                name = sorted(names, key=lambda n: (not n.lower().endswith(".srt"), n))[0]
                return zf.read(name), "." + name.rsplit(".", 1)[-1].lower()
        return data, ".srt"


PROVIDERS = {"opensubtitles": OpenSubtitles, "podnapisi": Podnapisi}


def build(cfgs: list[dict]) -> list:
    out = []
    for c in cfgs:
        cls = PROVIDERS.get(c.get("id"))
        if cls and c.get("enabled", True):
            p = cls(c)
            if p.configured():
                out.append(p)
    return out
