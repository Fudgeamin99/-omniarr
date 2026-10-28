"""qBittorrent Web API v2 client."""
from __future__ import annotations

import asyncio
import ntpath
import posixpath
from typing import Any

import httpx2 as httpx

from . import log, settings

L = log.get("qbittorrent")

COMPLETED_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP", "checkingUP"}
ERROR_STATES = {"error", "missingFiles"}
STALL_STATES = {"stalledDL", "metaDL", "forcedMetaDL"}


class QbitError(Exception):
    pass


class QbitClient:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or settings.get("qbittorrent")
        self.base = (self.cfg.get("url") or "").rstrip("/")
        self._sid: str | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), trust_env=False)
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict:
        h = {"Referer": self.base, "Origin": self.base}
        if self._sid:
            h["Cookie"] = f"SID={self._sid}"
        return h

    async def login(self) -> None:
        if not self.base:
            raise QbitError("No qBittorrent address set.")
        try:
            r = await self._client.post(f"{self.base}/api/v2/auth/login", headers=self._headers(),
                                        data={"username": self.cfg.get("username") or "",
                                              "password": self.cfg.get("password") or ""})
        except httpx.HTTPError as e:
            raise QbitError(f"Couldn't reach qBittorrent at {self.base} — is Web UI turned on? "
                            f"({e.__class__.__name__})") from e
        if r.status_code == 403:
            raise QbitError("qBittorrent has temporarily banned this address after failed logins. "
                            "Wait a while or restart qBittorrent.")
        if r.status_code == 404:
            raise QbitError("That address doesn't look like qBittorrent's Web UI.")
        text = r.text.strip()
        sid = r.cookies.get("SID") or r.cookies.get("QBT_SID") or None
        if not sid:
            for raw in r.headers.get_list("set-cookie"):
                if raw.startswith("SID=") or "SID=" in raw:
                    sid = raw.split("SID=", 1)[1].split(";", 1)[0]
        if text.startswith("Fails"):
            raise QbitError("qBittorrent rejected the username or password.")
        if r.status_code >= 400:
            raise QbitError(f"qBittorrent login failed (HTTP {r.status_code}).")
        self._sid = sid  # may be None when auth bypass for local network is on

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        async with self._lock:
            if self._sid is None and not getattr(self, "_tried", False):
                await self.login()
                self._tried = True
        try:
            r = await self._client.request(method, f"{self.base}/api/v2/{path}", headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise QbitError(f"Couldn't reach qBittorrent ({e.__class__.__name__}).") from e
        if r.status_code == 403:
            await self.login()
            r = await self._client.request(method, f"{self.base}/api/v2/{path}", headers=self._headers(), **kw)
        if r.status_code >= 400:
            raise QbitError(f"qBittorrent error on {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    async def version(self) -> str:
        r = await self._req("GET", "app/version")
        return r.text.strip()

    async def preferences(self) -> dict:
        return (await self._req("GET", "app/preferences")).json()

    async def ensure_category(self, name: str) -> None:
        cats = (await self._req("GET", "torrents/categories")).json() or {}
        if name not in cats:
            try:
                await self._req("POST", "torrents/createCategory", data={"category": name, "savePath": ""})
            except QbitError as e:
                if "409" not in str(e):
                    raise

    async def add(self, *, magnet: str | None = None, torrent: bytes | None = None, category: str,
                  tags: str = "omniarr") -> None:
        data = {"category": category, "tags": tags, "paused": "false", "stopped": "false",
                "autoTMM": "true"}
        if magnet:
            data["urls"] = magnet
            r = await self._req("POST", "torrents/add", data=data)
        elif torrent:
            r = await self._req("POST", "torrents/add", data=data,
                                files={"torrents": ("release.torrent", torrent, "application/x-bittorrent")})
        else:
            raise QbitError("Nothing to add")
        if r.text.strip().startswith("Fails"):
            raise QbitError("qBittorrent refused the torrent (it may already be in the list).")

    async def torrents(self, category: str | None = None, hashes: list[str] | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if hashes:
            params["hashes"] = "|".join(hashes)
        return (await self._req("GET", "torrents/info", params=params)).json()

    async def files(self, h: str) -> list[dict]:
        return (await self._req("GET", "torrents/files", params={"hash": h})).json()

    async def delete(self, h: str, delete_files: bool = False) -> None:
        await self._req("POST", "torrents/delete", data={"hashes": h, "deleteFiles": "true" if delete_files
                                                         else "false"})

    async def set_share_limits(self, h: str, ratio: float | None, seed_minutes: int | None) -> None:
        await self._req("POST", "torrents/setShareLimits", data={
            "hashes": h, "ratioLimit": ratio if ratio is not None else -2,
            "seedingTimeLimit": seed_minutes if seed_minutes is not None else -2,
            "inactiveSeedingTimeLimit": -2})

    async def wait_for(self, h: str, timeout: float = 15.0) -> dict | None:
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            items = await self.torrents(hashes=[h])
            if items:
                return items[0]
            await asyncio.sleep(0.5)
        return None


def map_path(remote: str) -> str:
    """Translate a path as qBittorrent sees it into a path inside this container."""
    if not remote:
        return remote
    mappings = settings.get("qbittorrent").get("path_mappings") or []
    norm = remote.replace("\\", "/")
    for m in sorted(mappings, key=lambda m: -len(m.get("remote") or "")):
        r = (m.get("remote") or "").replace("\\", "/")
        loc = m.get("local") or ""
        if not r:
            continue
        r_cmp = r if r.endswith("/") else r + "/"
        n_cmp = norm if norm.endswith("/") else norm + "/"
        windows = bool(ntpath.splitdrive(r)[0])
        if (n_cmp.lower() if windows else n_cmp).startswith(r_cmp.lower() if windows else r_cmp):
            rest = norm[len(r.rstrip("/")):].lstrip("/")
            return posixpath.join(loc, rest) if rest else loc.rstrip("/") or loc
    return norm


_client: QbitClient | None = None


def get_client() -> QbitClient:
    global _client
    cfg = settings.get("qbittorrent")
    if _client is None or _client.cfg != cfg:
        _client = QbitClient(cfg)
    return _client


def reset_client() -> None:
    global _client
    _client = None
