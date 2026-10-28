"""Generic Torznab (and Newznab-style torrent) API client."""
from __future__ import annotations

import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..net import IndexerError, Session
from . import base as B


class TorznabIndexer:
    def __init__(self, indexer_id: int, name: str, cfg: dict, session: Session, priority: int = 25):
        self.id = indexer_id
        self.name = name
        self.cfg = cfg
        self.session = session
        self.session.polite_by_host = False   # one Jackett/Prowlarr host can serve many sites
        self.priority = priority
        self._caps: dict | None = None

    @property
    def api_url(self) -> str:
        url = (self.cfg.get("url") or "").strip().rstrip("/")
        path = urlparse(url).path
        return url if path.endswith("/api") else url + "/api"

    def _params(self, extra: dict) -> dict:
        p = dict(extra)
        if self.cfg.get("api_key"):
            p["apikey"] = self.cfg["api_key"]
        return p

    async def caps(self) -> dict:
        if self._caps is not None:
            return self._caps
        r = await self.session.request("GET", self.api_url, params=self._params({"t": "caps"}))
        if r.status in (401, 403):
            raise IndexerError("auth", "The API key was rejected.")
        if r.status >= 400:
            raise IndexerError("http", f"The indexer answered HTTP {r.status}.")
        soup = BeautifulSoup(r.text, "xml")
        err = soup.find("error")
        if err is not None:
            raise IndexerError("auth" if err.get("code") in ("100", "101", "102") else "http",
                               err.get("description") or "The indexer reported an error.")
        caps: dict = {"tv": [], "movie": [], "search": ["q"], "cats": []}
        searching = soup.find("searching")
        if searching:
            for tag, key in (("tv-search", "tv"), ("movie-search", "movie"), ("search", "search")):
                el = searching.find(tag)
                if el is not None and el.get("available", "yes") == "yes":
                    caps[key] = [x.strip() for x in (el.get("supportedParams") or "q").split(",")]
        for c in soup.find_all("category"):
            try:
                caps["cats"].append(int(c.get("id")))
            except (TypeError, ValueError):
                pass
        self._caps = caps
        return caps

    def build_query(self, kind: str, keywords: str, season: int | None = None, ep: int | None = None,
                    imdb_id: str | None = None, tvdb_id: int | None = None, tmdb_id: int | None = None,
                    year: int | None = None) -> dict:
        return {"kind": kind, "keywords": keywords, "season": season, "ep": ep, "imdb_id": imdb_id,
                "tvdb_id": tvdb_id, "tmdb_id": tmdb_id, "year": year,
                "categories": [5000] if kind == "tv" else [2000] if kind == "movies" else []}

    async def search(self, q: dict) -> list[B.Release]:
        caps = await self.caps()
        kind = q.get("kind")
        params: dict = {"extended": 1, "limit": 100}
        if kind == "tv" and caps.get("tv"):
            params["t"] = "tvsearch"
            sup = caps["tv"]
            cats = [c for c in caps["cats"] if 5000 <= c < 6000] or [5000]
            params["cat"] = ",".join(str(c) for c in cats[:20])
            kw = q.get("keywords") or ""
            # When season/ep params are supported, send the bare title + params
            if q.get("season") is not None and "season" in sup:
                params["season"] = q["season"]
                kw = q.get("title_only", kw)
                if q.get("ep") is not None and "ep" in sup:
                    params["ep"] = q["ep"]
            if q.get("tvdb_id") and "tvdbid" in sup:
                params["tvdbid"] = q["tvdb_id"]
            if q.get("imdb_id") and "imdbid" in sup:
                params["imdbid"] = q["imdb_id"]
            if kw:
                params["q"] = kw
        elif kind == "movies" and caps.get("movie"):
            params["t"] = "movie"
            sup = caps["movie"]
            cats = [c for c in caps["cats"] if 2000 <= c < 3000] or [2000]
            params["cat"] = ",".join(str(c) for c in cats[:20])
            if q.get("imdb_id") and "imdbid" in sup:
                params["imdbid"] = q["imdb_id"].replace("tt", "")
            if q.get("tmdb_id") and "tmdbid" in sup:
                params["tmdbid"] = q["tmdb_id"]
            if q.get("keywords"):
                params["q"] = q["keywords"]
        else:
            params["t"] = "search"
            if q.get("keywords"):
                params["q"] = q["keywords"]
            if kind == "tv":
                params["cat"] = "5000"
            elif kind == "movies":
                params["cat"] = "2000"
        r = await self.session.request("GET", self.api_url, params=self._params(params))
        if r.status >= 400:
            raise IndexerError("auth" if r.status in (401, 403) else "http", f"The indexer answered HTTP {r.status}.")
        return self.parse(r.text)

    def parse(self, xml: str) -> list[B.Release]:
        soup = BeautifulSoup(xml, "xml")
        err = soup.find("error")
        if err is not None and not soup.find("item"):
            raise IndexerError("http", err.get("description") or "The indexer reported an error.")
        out = []
        for item in soup.find_all("item"):
            attrs: dict[str, list[str]] = {}
            for a in item.find_all("attr"):
                attrs.setdefault((a.get("name") or "").lower(), []).append(a.get("value") or "")
            def at(n):
                v = attrs.get(n)
                return v[0] if v else None
            title = (item.title.text if item.title else "").strip()
            if not title:
                continue
            enclosure = item.find("enclosure")
            link = (item.link.text.strip() if item.link and item.link.text else None)
            dl = enclosure.get("url") if enclosure is not None and enclosure.get("url") else link
            magnet = at("magneturl")
            if dl and dl.startswith("magnet:"):
                magnet, dl = dl, None
            size = B.parse_int(at("size")) or (B.parse_int(item.size.text) if item.find("size") else None) or \
                (B.parse_int(enclosure.get("length")) if enclosure is not None else None)
            cats = []
            for c in attrs.get("category", []) + [x.text for x in item.find_all("category")]:
                n = B.parse_int(c)
                if n:
                    cats.append(n)
            seeders = B.parse_int(at("seeders"))
            peers = B.parse_int(at("peers"))
            mst = B.parse_float(at("minimumseedtime"))
            out.append(B.Release(
                title=title, indexer_id=self.id, indexer_name=self.name,
                guid=(item.guid.text.strip() if item.guid and item.guid.text else (dl or magnet or title)),
                download_url=dl, magnet=magnet, info_hash=(at("infohash") or "").lower() or None,
                details_url=(item.comments.text if item.find("comments") else None) or link,
                size=size, seeders=seeders, leechers=(peers - seeders) if peers is not None and seeders is not None
                else B.parse_int(at("leechers")),
                grabs=B.parse_int(at("grabs")), files=B.parse_int(at("files")),
                published=B.parse_date(item.pubDate.text) if item.find("pubDate") else time.time(),
                categories=sorted(set(cats)), imdb_id=B.normalize_imdb(at("imdb") or at("imdbid")),
                tvdb_id=B.parse_int(at("tvdbid")), tmdb_id=B.parse_int(at("tmdbid")),
                min_ratio=B.parse_float(at("minimumratio")), min_seed_time=int(mst) if mst else None,
                download_factor=B.parse_float(at("downloadvolumefactor")), indexer_priority=self.priority,
            ))
        return out

    async def fetch_torrent(self, rel: B.Release) -> tuple[bytes | None, str | None]:
        if not rel.download_url:
            return None, rel.magnet
        r = await self.session.request("GET", rel.download_url, headers={"Accept": "application/x-bittorrent,*/*"})
        if r.redirect_to:
            return None, r.redirect_to
        from ..torrent import is_torrent
        if is_torrent(r.content):
            return r.content, None
        if rel.magnet:
            return None, rel.magnet
        raise IndexerError("http", f"Download failed (HTTP {r.status}, not a torrent file).")

    async def test(self) -> int:
        await self.caps()
        return len(await self.search({"kind": None, "keywords": ""}))
