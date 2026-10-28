"""Stand-in services for end-to-end testing.

One Starlette app emulating: TVMaze, TMDB, a Torznab indexer, a private HTML tracker with a login form,
a JSON site behind a Cloudflare check, FlareSolverr, an ISP-blocked site, qBittorrent's Web API,
OpenSubtitles.com and Podnapisi. Run: python -m tests.mocks --port 18999 --dir /tmp/x
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from omniarr.torrent import encode, info_hash

PORT = 18999
HOST = os.environ.get("MOCK_HOST", "127.0.0.1")
ROOT = Path("/tmp/omniarr-mock")
FIX = Path(__import__("tempfile").gettempdir()) / f"omniarr-mock-fixtures-{os.getuid()}"
STATE: dict = {"torrents": {}, "registry": {}, "requests": [], "qb_cookie": "SIDmock", "categories": {},
               "share_limits": {}, "deleted": []}


def today(offset: int = 0) -> str:
    return (dt.date.today() + dt.timedelta(days=offset)).isoformat()


# ------------------------------------------------------------------- data ---
SHOWS = {
    1: {"id": 1, "name": "Glass Harbor", "premiered": today(-400), "status": "Running", "language": "English",
        "type": "Scripted", "genres": ["Drama"], "averageRuntime": 50, "summary": "<p>A harbour town keeps secrets.</p>",
        "network": {"name": "HBO", "country": {"code": "US"}}, "image": None,
        "externals": {"imdb": "tt1000001", "thetvdb": 9001}},
    2: {"id": 2, "name": "Frieren", "premiered": today(-300), "status": "Running", "language": "Japanese",
        "type": "Animation", "genres": ["Anime", "Fantasy"], "averageRuntime": 24, "summary": "<p>An elf mage.</p>",
        "network": {"name": "NTV", "country": {"code": "JP"}}, "image": None,
        "externals": {"imdb": "tt1000002", "thetvdb": 9002}},
    3: {"id": 3, "name": "Signal Fire", "premiered": today(-60), "status": "Running", "language": "English",
        "type": "Scripted", "genres": ["Thriller"], "averageRuntime": 45, "summary": "<p>A lighthouse signal.</p>",
        "network": {"name": "BBC One", "country": {"code": "GB"}}, "image": None,
        "externals": {"imdb": "tt1000003", "thetvdb": 9003}},
}
EPISODES = {
    1: [
        {"id": 101, "season": 1, "number": 1, "name": "Return to Port", "airdate": today(-400), "type": "regular"},
        {"id": 102, "season": 1, "number": 2, "name": "Dry Dock", "airdate": today(-393), "type": "regular"},
        {"id": 103, "season": 1, "number": 3, "name": "Lighthouse Keeper", "airdate": today(-386), "type": "regular"},
        {"id": 201, "season": 2, "number": 1, "name": "The Breakwater", "airdate": today(-21), "type": "regular"},
        {"id": 202, "season": 2, "number": 2, "name": "Low Tide", "airdate": today(-14), "type": "regular"},
        {"id": 203, "season": 2, "number": 3, "name": "Unmarked Graves", "airdate": today(7), "type": "regular"},
        {"id": 199, "season": 1, "number": None, "name": "Behind the Harbor", "airdate": today(-380),
         "type": "significant_special"},
    ],
    2: [
        {"id": 301, "season": 1, "number": 1, "name": "The Journey's End", "airdate": today(-300), "type": "regular"},
        {"id": 302, "season": 1, "number": 2, "name": "It Didn't Have to Be Magic", "airdate": today(-293),
         "type": "regular"},
    ],
    3: [
        {"id": 401, "season": 1, "number": 1, "name": "Beacon", "airdate": today(-60), "type": "regular"},
        {"id": 402, "season": 1, "number": 2, "name": "Static", "airdate": today(-1), "type": "regular"},
    ],
}
MOVIES = {
    101: {"id": 101, "title": "Northbound", "original_title": "Northbound", "release_date": "2025-03-01",
          "original_language": "en", "overview": "A long drive north.", "runtime": 118, "status": "Released",
          "imdb_id": "tt2000101", "digital": "2025-06-01"},
    102: {"id": 102, "title": "Red Meridian", "original_title": "Red Meridian", "release_date": "2022-04-01",
          "original_language": "en", "overview": "A ship goes missing.", "runtime": 104, "status": "Released",
          "imdb_id": "tt2000102", "digital": "2022-07-01"},
    103: {"id": 103, "title": "Paper Lanterns", "original_title": "Paper Lanterns", "release_date": today(120),
          "original_language": "en", "overview": "Not out yet.", "runtime": 99, "status": "Post Production",
          "imdb_id": "tt2000103", "digital": None},
}

GB = 1_000_000_000
# site: torznab | omnitest | cf | blocked ; rss: shows up in the "latest" feed
RELEASES = [
    # Glass Harbor season 1 pack (torznab) + a foreign-language pack that must be rejected
    {"id": 1, "site": "torznab", "title": "Glass.Harbor.S01.1080p.WEB-DL.DDP5.1.H.264-NTb", "size": 6 * GB,
     "seeders": 120, "cat": 5040, "files": [f"Glass.Harbor.S01E0{n}.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv"
                                           for n in (1, 2, 3)] + ["Sample/glass.harbor.s01e01.sample.mkv"]},
    {"id": 2, "site": "torznab", "title": "Glass.Harbor.S01.FRENCH.1080p.WEB.H264-FRGRP", "size": 6 * GB,
     "seeders": 500, "cat": 5040, "files": ["glass.s01.french.mkv"]},
    # S02E01 on the private tracker (login required) — and a 720p copy on torznab with more seeders
    {"id": 3, "site": "omnitest", "title": "Glass.Harbor.S02E01.1080p.WEB-DL.DDP5.1.H.264-NTb", "size": 2 * GB,
     "seeders": 40, "cat": "1", "files": ["Glass.Harbor.S02E01.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv"],
     "freeleech": True},
    {"id": 4, "site": "torznab", "title": "Glass.Harbor.S02E01.720p.HDTV.x264-SVA", "size": 1 * GB,
     "seeders": 900, "cat": 5040, "files": ["glass.harbor.s02e01.720p.hdtv.x264-sva.mkv"]},
    # S02E02: German-only (rejected), English with embedded subs (accepted), a CAM-ish fake
    {"id": 5, "site": "torznab", "title": "Glass.Harbor.S02E02.GERMAN.1080p.WEB.H264-GER", "size": 2 * GB,
     "seeders": 300, "cat": 5040, "files": ["gh.s02e02.german.mkv"]},
    {"id": 6, "site": "omnitest", "title": "Glass.Harbor.S02E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX",
     "size": 2 * GB, "seeders": 25, "cat": "1",
     "files": ["Glass.Harbor.S02E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX.mkv"], "embedded_subs": True},
    {"id": 7, "site": "torznab", "title": "Glass.Harbor.S02E02.1080p.WEB-DL-TINY", "size": 30_000_000,
     "seeders": 999, "cat": 5040, "files": ["tiny.mkv"]},
    # Frieren (anime): subs-only (reject), English dub (accept)
    {"id": 8, "site": "torznab", "title": "[SubsPlease] Frieren - 01 (1080p) [AAAA1111].mkv", "size": 1.4 * GB,
     "seeders": 800, "cat": 5070, "files": ["[SubsPlease] Frieren - 01 (1080p) [AAAA1111].mkv"]},
    {"id": 9, "site": "torznab", "title": "[DubGroup] Frieren - 01 [English Dub][1080p WEB-DL]", "size": 1.3 * GB,
     "seeders": 60, "cat": 5070, "files": ["[DubGroup] Frieren - 01 [English Dub][1080p WEB-DL].mkv"]},
    # Northbound movie only on the Cloudflare site (magnet via infohash)
    {"id": 10, "site": "cf", "title": "Northbound.2025.1080p.WEB-DL.DDP5.1.H.264-FLUX", "size": 8 * GB,
     "seeders": 150, "cat": "movies", "files": ["Northbound.2025.1080p.WEB-DL.DDP5.1.H.264-FLUX/Northbound.2025.1080p.WEB-DL.DDP5.1.H.264-FLUX.mkv",
                                                "Northbound.2025.1080p.WEB-DL.DDP5.1.H.264-FLUX/RARBG.txt"]},
    {"id": 11, "site": "cf", "title": "Northbound.2025.HDCAM.x264-CAMGRP", "size": 2 * GB, "seeders": 2000,
     "cat": "movies", "files": ["northbound.cam.mkv"]},
    # Red Meridian: best one stalls, second one is fine (tests fail → blocklist → re-search)
    {"id": 12, "site": "torznab", "title": "Red.Meridian.2022.1080p.BluRay.x264-STALLTEST", "size": 9 * GB,
     "seeders": 50, "cat": 2040, "files": ["Red.Meridian.2022.1080p.BluRay.x264-STALLTEST.mkv"]},
    {"id": 13, "site": "torznab", "title": "Red.Meridian.2022.1080p.WEB-DL.DDP5.1.H.264-GOOD", "size": 7 * GB,
     "seeders": 30, "cat": 2040, "files": ["Red.Meridian.2022.1080p.WEB-DL.DDP5.1.H.264-GOOD.mkv"]},
    # Signal Fire S01E02 appears only in the RSS feed later
    {"id": 14, "site": "torznab", "title": "Signal.Fire.S01E02.1080p.WEB.H264-RSSGRP", "size": 2.5 * GB,
     "seeders": 70, "cat": 5040, "files": ["Signal.Fire.S01E02.1080p.WEB.H264-RSSGRP.mkv"], "rss_only": True,
     "hidden": True},
    {"id": 15, "site": "torznab", "title": "Signal.Fire.S01E01.1080p.WEB.H264-RSSGRP", "size": 2.5 * GB,
     "seeders": 70, "cat": 5040, "files": ["Signal.Fire.S01E01.1080p.WEB.H264-RSSGRP.mkv"]},
    # A REPACK of S02E01 shows up later in RSS → upgrade replaces the old file
    {"id": 17, "site": "torznab", "title": "Glass.Harbor.S02E01.REPACK.1080p.WEB-DL.DDP5.1.H.264-NTb", "size": 2 * GB,
     "seeders": 80, "cat": 5040, "files": ["Glass.Harbor.S02E01.REPACK.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv"],
     "rss_only": True, "hidden": True},
    # Music on the private tracker (must be ignored)
    {"id": 16, "site": "omnitest", "title": "Glass Harbor OST 2025 FLAC", "size": 300_000_000, "seeders": 5,
     "cat": "3", "files": ["ost.flac"]},
]


def _files_for(rel: dict) -> tuple[str, list[tuple[str, int]]]:
    files = rel["files"]
    if len(files) == 1 and "/" not in files[0]:
        return files[0], [(files[0], int(rel["size"]))]
    if all("/" in f for f in files):
        root = files[0].split("/")[0]
        inner = [f.split("/", 1)[1] for f in files]
    else:
        root = rel["title"]
        inner = files
    per = int(rel["size"]) // max(1, len(inner))
    return root, [(f, per) for f in inner]


def torrent_for(rel: dict) -> tuple[bytes, str]:
    root, files = _files_for(rel)
    if len(files) == 1 and files[0][0] == root:
        info = {"name": root, "length": files[0][1], "piece length": 262144, "pieces": b"\x00" * 20}
    else:
        info = {"name": root, "piece length": 262144, "pieces": b"\x00" * 20,
                "files": [{"length": size, "path": path.split("/")} for path, size in files]}
    meta = {"announce": "http://tracker.invalid/announce", "info": info}
    data = encode(meta)
    h = info_hash(data)
    STATE["registry"][h] = {"rel": rel, "root": root, "files": files, "single": "files" not in info}
    return data, h


for _r in RELEASES:
    torrent_for(_r)
_hash_by_id = {v["rel"]["id"]: h for h, v in STATE["registry"].items()}


def log_req(site: str, request: Request) -> None:
    STATE["requests"].append({"t": time.time(), "site": site, "path": request.url.path,
                              "query": str(request.url.query)})


# ---------------------------------------------------------------- tvmaze ---
async def tvm_search(request: Request):
    q = request.query_params.get("q", "").lower()
    return JSONResponse([{"score": 1, "show": s} for s in SHOWS.values() if q.split()[0] in s["name"].lower()])


async def tvm_show(request: Request):
    return JSONResponse(SHOWS[int(request.path_params["id"])])


async def tvm_eps(request: Request):
    out = []
    for e in EPISODES[int(request.path_params["id"])]:
        out.append({**e, "airstamp": e["airdate"] + "T01:00:00+00:00", "runtime": 50, "summary": "<p>Episode.</p>"})
    return JSONResponse(out)


async def tvm_akas(request: Request):
    return JSONResponse([])


# ------------------------------------------------------------------ tmdb ---
def _tmdb_ok(request: Request) -> bool:
    return request.query_params.get("api_key") == "tmdbkey" or \
        request.headers.get("authorization") == "Bearer " + "x" * 50


async def tmdb_config(request: Request):
    if not _tmdb_ok(request):
        return JSONResponse({"status_message": "Invalid API key"}, status_code=401)
    return JSONResponse({"images": {}})


async def tmdb_search(request: Request):
    if not _tmdb_ok(request):
        return JSONResponse({"status_message": "Invalid API key"}, status_code=401)
    q = request.query_params.get("query", "").lower()
    res = [{"id": m["id"], "title": m["title"], "original_title": m["original_title"],
            "release_date": m["release_date"], "overview": m["overview"], "poster_path": None,
            "original_language": m["original_language"]} for m in MOVIES.values() if q.split()[0] in m["title"].lower()]
    return JSONResponse({"results": res})


async def tmdb_movie(request: Request):
    if not _tmdb_ok(request):
        return JSONResponse({"status_message": "Invalid API key"}, status_code=401)
    m = MOVIES[int(request.path_params["id"])]
    rd = [{"iso_3166_1": "US", "release_dates": [{"type": 3, "release_date": m["release_date"] + "T00:00:00.000Z"}]
           + ([{"type": 4, "release_date": m["digital"] + "T00:00:00.000Z"}] if m["digital"] else [])}]
    return JSONResponse({"id": m["id"], "title": m["title"], "original_title": m["original_title"],
                         "release_date": m["release_date"], "overview": m["overview"], "poster_path": None,
                         "backdrop_path": None, "original_language": m["original_language"], "runtime": m["runtime"],
                         "status": m["status"], "imdb_id": m["imdb_id"], "genres": [{"name": "Drama"}],
                         "release_dates": {"results": rd}, "alternative_titles": {"titles": []}})


# --------------------------------------------------------------- torznab ---
def _visible(rel: dict) -> bool:
    return not rel.get("hidden")


def _match(rel: dict, q: str) -> bool:
    words = [w for w in q.lower().replace(".", " ").split() if w]
    t = rel["title"].lower().replace(".", " ").replace("-", " ")
    return all(w in t for w in words)


async def torznab(request: Request):
    log_req("torznab", request)
    p = request.query_params
    if p.get("apikey") != "tzkey":
        return Response('<?xml version="1.0"?><error code="100" description="Incorrect user credentials"/>',
                        media_type="application/xml")
    t = p.get("t")
    if t == "caps":
        return Response("""<?xml version="1.0" encoding="UTF-8"?><caps><searching>
<search available="yes" supportedParams="q"/><tv-search available="yes" supportedParams="q,season,ep"/>
<movie-search available="yes" supportedParams="q,imdbid"/></searching><categories>
<category id="2000" name="Movies"><subcat id="2040" name="Movies/HD"/></category>
<category id="5000" name="TV"><subcat id="5040" name="TV/HD"/><subcat id="5070" name="TV/Anime"/></category>
</categories></caps>""", media_type="application/xml")
    q = p.get("q", "")
    items = []
    for r in RELEASES:
        if r["site"] != "torznab" or not _visible(r):
            continue
        if not q:
            pass  # RSS: everything recent
        elif r.get("rss_only"):
            continue
        else:
            if not _match(r, q):
                continue
            if t == "tvsearch" and p.get("season"):
                s = int(p["season"])
                tl = r["title"].lower()
                if f"s{s:02d}" not in tl and r["cat"] != 5070:
                    continue
                if p.get("ep") and f"e{int(p['ep']):02d}" not in tl and r["cat"] != 5070:
                    continue
        cats = p.get("cat", "")
        if cats and str(r["cat"]) not in cats.split(",") and str(r["cat"])[0] + "000" not in cats.split(","):
            continue
        h = _hash_by_id[r["id"]]
        items.append(f"""<item><title>{r['title'].replace('&', '&amp;')}</title><guid>torznab-{r['id']}</guid>
<link>http://{HOST}:{PORT}/torznab/dl/{r['id']}?apikey=tzkey</link>
<comments>http://{HOST}:{PORT}/torznab/details/{r['id']}</comments>
<pubDate>{dt.datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate><size>{int(r['size'])}</size>
<enclosure url="http://{HOST}:{PORT}/torznab/dl/{r['id']}?apikey=tzkey" length="{int(r['size'])}" type="application/x-bittorrent"/>
<torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="category" value="{r['cat']}"/>
<torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="seeders" value="{r['seeders']}"/>
<torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="peers" value="{r['seeders'] + 5}"/>
<torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="infohash" value="{h}"/>
<torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="minimumratio" value="1.0"/>
<torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="minimumseedtime" value="3600"/>
</item>""")
    return Response(f"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>{''.join(items)}</channel></rss>""",
                    media_type="application/rss+xml")


async def torznab_dl(request: Request):
    log_req("torznab-dl", request)
    rel = next(r for r in RELEASES if r["id"] == int(request.path_params["id"]))
    data, _ = torrent_for(rel)
    return Response(data, media_type="application/x-bittorrent")


# ---------------------------------------------------- private html tracker ---
LOGIN_FORM = """<html><body><form id="login" action="takelogin.php" method="post">
<input type="hidden" name="csrf" value="tok123"><input name="username"><input type="password" name="password">
<input type="checkbox" name="remember" value="1"><input type="submit" value="Log in"></form>{err}</body></html>"""


def _logged_in(request: Request) -> bool:
    return request.cookies.get("uid") == "7" and request.cookies.get("pass") == "secrethash"


async def site_login_page(request: Request):
    log_req("omnitest", request)
    return HTMLResponse(LOGIN_FORM.format(err=""))


async def site_takelogin(request: Request):
    log_req("omnitest", request)
    form = parse_qs((await request.body()).decode())
    if form.get("csrf") != ["tok123"]:
        return HTMLResponse(LOGIN_FORM.format(err='<div class="error">Bad form token</div>'))
    if form.get("username") != ["omni"] or form.get("password") != ["hunter2"]:
        return HTMLResponse(LOGIN_FORM.format(err='<div class="error">Wrong username or password</div>'))
    r = RedirectResponse("index.php", status_code=302)
    r.set_cookie("uid", "7")
    r.set_cookie("pass", "secrethash")
    return r


async def site_index(request: Request):
    log_req("omnitest", request)
    if not _logged_in(request):
        return HTMLResponse(LOGIN_FORM.format(err=""))
    return HTMLResponse('<html><body>Welcome <a href="logout.php">Log out</a></body></html>')


async def site_browse(request: Request):
    log_req("omnitest", request)
    if not _logged_in(request):
        return HTMLResponse(LOGIN_FORM.format(err=""))
    q = request.query_params.get("q", "")
    rows = []
    for r in RELEASES:
        if r["site"] != "omnitest" or (q and not _match(r, q)):
            continue
        cat_ok = any(request.query_params.get(f"c{c}") for c in ("1", "2", "3"))
        if cat_ok and not request.query_params.get(f"c{r['cat']}"):
            continue
        added = (dt.datetime.utcnow() - dt.timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        size_txt = f"{r['size'] / 1024 ** 3:.2f} GB"
        fl = '<img class="freeleech" src="fl.png">' if r.get("freeleech") else ""
        rows.append(f"""<tr><td><a href="browse.php?cat={r['cat']}"><img src="cat.png"></a></td>
<td><a href="details.php?id={r['id']}">{r['title']}</a> {fl}<a href="download.php?id={r['id']}">DL</a></td>
<td>{added}</td><td>{size_txt}</td><td>{r['seeders']:,}</td><td>3</td></tr>""")
    return HTMLResponse(f"""<html><body><a href="logout.php">Log out</a><table class="torrents"><thead><tr><th>c</th>
<th>Name</th><th>Added</th><th>Size</th><th>S</th><th>L</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>""")


async def site_download(request: Request):
    log_req("omnitest-dl", request)
    if not _logged_in(request):
        return HTMLResponse(LOGIN_FORM.format(err=""), status_code=200)
    rel = next(r for r in RELEASES if r["id"] == int(request.query_params["id"]))
    data, _ = torrent_for(rel)
    return Response(data, media_type="application/x-bittorrent")


# ------------------------------------------------------- cloudflare site ----
CF_PAGE = "<html><head><title>Just a moment...</title></head><body>Checking your browser... cf-chl</body></html>"


async def cf_search(request: Request):
    log_req("cf", request)
    if request.cookies.get("cf_clearance") != "granted" or request.headers.get("user-agent") != "FS-UA/1.0":
        return HTMLResponse(CF_PAGE, status_code=503, headers={"server": "cloudflare"})
    q = request.query_params.get("q", "")
    out = []
    for r in RELEASES:
        if r["site"] != "cf" or (q and not _match(r, q)):
            continue
        out.append({"name": r["title"], "size": int(r["size"]), "seeders": r["seeders"], "leechers": 4,
                    "info_hash": _hash_by_id[r["id"]], "added": int(time.time()) - 7200, "category": r["cat"]})
    return JSONResponse({"data": {"torrents": out}})


async def fs_index(request: Request):
    return JSONResponse({"msg": "FlareSolverr is ready!", "version": "3.3.21", "userAgent": "FS-UA/1.0"})


async def fs_v1(request: Request):
    payload = await request.json()
    STATE["requests"].append({"t": time.time(), "site": "flaresolverr", "path": payload.get("url"), "query": ""})
    return JSONResponse({"status": "ok", "message": "Challenge solved!", "solution": {
        "url": payload["url"], "status": 200, "response": "<html><body><pre>{}</pre></body></html>",
        "cookies": [{"name": "cf_clearance", "value": "granted"}], "userAgent": "FS-UA/1.0"}})


async def blocked_site(request: Request):
    log_req("blocked", request)
    return HTMLResponse("<html><body><h1>This site has been blocked</h1><p>Access to this website has been blocked "
                        "under an order of the High Court.</p></body></html>")


# ---------------------------------------------------------- qBittorrent ---
def _qb_auth(request: Request) -> bool:
    return request.cookies.get("SID") == STATE["qb_cookie"]


async def qb_login(request: Request):
    form = parse_qs((await request.body()).decode())
    if form.get("username") == ["admin"] and form.get("password") == ["adminpass"]:
        r = PlainTextResponse("Ok.")
        r.set_cookie("SID", STATE["qb_cookie"])
        return r
    return PlainTextResponse("Fails.")


def _need_auth(fn):
    async def w(request: Request):
        if not _qb_auth(request):
            return PlainTextResponse("Forbidden", status_code=403)
        return await fn(request)
    return w


@_need_auth
async def qb_version(request):
    return PlainTextResponse("v4.6.7")


@_need_auth
async def qb_prefs(request):
    return JSONResponse({"save_path": str(ROOT / "data" / "torrents") + "/"})


@_need_auth
async def qb_categories(request):
    return JSONResponse({k: {"name": k, "savePath": ""} for k in STATE["categories"]})


@_need_auth
async def qb_create_category(request):
    form = parse_qs((await request.body()).decode())
    STATE["categories"][form["category"][0]] = True
    return PlainTextResponse("")


@_need_auth
async def qb_add(request):
    form = await request.form()
    cat = form.get("category") or ""
    hashes = []
    if form.get("urls"):
        from omniarr.torrent import magnet_hash
        for u in str(form["urls"]).split("\n"):
            hashes.append(magnet_hash(u))
    if form.get("torrents") is not None:
        data = await form["torrents"].read()
        hashes.append(info_hash(data))
    for h in hashes:
        if h in STATE["torrents"]:
            return PlainTextResponse("Fails.")
        reg = STATE["registry"].get(h)
        if not reg:
            return PlainTextResponse("Fails.")
        STATE["torrents"][h] = {"hash": h, "reg": reg, "category": cat, "added": time.time(), "progress": 0.0,
                                "state": "metaDL", "files_written": False}
    return PlainTextResponse("Ok.")


def _fixture_mkv() -> bytes:
    p = FIX / "fixture-subs.mkv"
    if not p.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
                        "-f", "srt", "-i", str(FIX / "fixture.srt"), "-c:v", "libx264", "-c:s", "srt",
                        "-metadata:s:s:0", "language=eng", str(p)], check=True)
    return p.read_bytes()


def _write_files(t: dict) -> None:
    reg = t["reg"]
    base = ROOT / "data" / "torrents"
    for path, _size in reg["files"]:
        dest = base / path if reg["single"] else base / reg["root"] / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if reg["rel"].get("embedded_subs") and path.endswith(".mkv"):
            dest.write_bytes(_fixture_mkv())
        else:
            dest.write_bytes(hashlib.sha256(path.encode()).digest() * 8192)   # 256 KiB
    t["files_written"] = True


def _info(t: dict) -> dict:
    age = time.time() - t["added"]
    reg = t["reg"]
    stall = "STALLTEST" in reg["rel"]["title"]
    if stall:
        t["state"], t["progress"] = "stalledDL", 0.0
    elif age > 1.5:
        if not t["files_written"]:
            _write_files(t)
        t["progress"], t["state"] = 1.0, "stalledUP" if age < 6 else "pausedUP"
    elif age > 0.3:
        t["progress"], t["state"] = 0.5, "downloading"
    save = str(ROOT / "data" / "torrents") + "/"
    return {"hash": t["hash"], "name": reg["root"], "progress": t["progress"], "state": t["state"],
            "category": t["category"], "save_path": save, "content_path": save + reg["root"],
            "size": int(reg["rel"]["size"]), "dlspeed": 5_000_000 if t["progress"] < 1 else 0,
            "eta": 30 if t["progress"] < 1 else 8640000, "ratio": 0.2 if age < 6 else 1.2,
            "seeding_time": max(0, int(age - 1.5)), "added_on": int(t["added"])}


@_need_auth
async def qb_info(request):
    cat = request.query_params.get("category")
    hashes = request.query_params.get("hashes")
    items = []
    for h, t in STATE["torrents"].items():
        if cat and t["category"] != cat:
            continue
        if hashes and h not in hashes.split("|"):
            continue
        items.append(_info(t))
    return JSONResponse(items)


@_need_auth
async def qb_delete(request):
    form = parse_qs((await request.body()).decode())
    for h in form["hashes"][0].split("|"):
        t = STATE["torrents"].pop(h, None)
        STATE["deleted"].append({"hash": h, "delete_files": form.get("deleteFiles") == ["true"]})
        if t and form.get("deleteFiles") == ["true"] and t["files_written"]:
            reg = t["reg"]
            base = ROOT / "data" / "torrents"
            target = base / reg["root"]
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
    return PlainTextResponse("")


@_need_auth
async def qb_share(request):
    form = parse_qs((await request.body()).decode())
    STATE["share_limits"][form["hashes"][0]] = {k: v[0] for k, v in form.items()}
    return PlainTextResponse("")


# -------------------------------------------------------- subtitles ---------
SRT = "1\n00:00:01,000 --> 00:00:03,000\nThe tide is turning.\n\n2\n00:00:04,000 --> 00:00:06,000\nCafé at noon?\n"


async def os_login(request: Request):
    if request.headers.get("api-key") != "oskey":
        return JSONResponse({"message": "bad key"}, status_code=403)
    body = await request.json()
    if body.get("username") != "osuser" or body.get("password") != "ospass":
        return JSONResponse({"message": "bad creds"}, status_code=401)
    return JSONResponse({"token": "ostoken", "user": {"allowed_downloads": 20}})


async def os_user(request: Request):
    return JSONResponse({"data": {"allowed_downloads": 20, "remaining_downloads": 18}})


async def os_formats(request: Request):
    return JSONResponse({"data": {"output_formats": ["srt"]}})


async def os_search(request: Request):
    STATE["requests"].append({"t": time.time(), "site": "opensubtitles", "path": "/subtitles",
                              "query": str(request.url.query)})
    if request.headers.get("api-key") != "oskey":
        return JSONResponse({"message": "bad key"}, status_code=403)
    p = request.query_params
    data = []
    if p.get("parent_imdb_id") == "1000001" and p.get("season_number") == "2" and p.get("episode_number") == "1":
        data.append({"attributes": {"language": "en", "release": "Glass.Harbor.S02E01.1080p.WEB-DL.DDP5.1.H.264-NTb",
                                    "hearing_impaired": False, "foreign_parts_only": False, "moviehash_match": False,
                                    "download_count": 4000, "files": [{"file_id": 501, "file_name": "gh201.srt"}]}})
        data.append({"attributes": {"language": "en", "release": "Glass.Harbor.S02E01.720p.HDTV.x264-SVA",
                                    "hearing_impaired": True, "files": [{"file_id": 502, "file_name": "x.srt"}]}})
    return JSONResponse({"data": data})


async def os_download(request: Request):
    body = await request.json()
    return JSONResponse({"link": f"http://{HOST}:{PORT}/os/file/{body['file_id']}", "remaining": 17})


async def os_file(request: Request):
    return PlainTextResponse(SRT.replace("tide", f"OS-{request.path_params['id']} tide"))


async def pod_search(request: Request):
    STATE["requests"].append({"t": time.time(), "site": "podnapisi", "path": "/search", "query": str(request.url.query)})
    p = request.query_params
    kw = p.get("keywords", "").lower()
    items = []
    if "glass" in kw:
        s, e = p.get("seasons"), p.get("episodes")
        items.append({"id": f"gh{s}{e}", "language": "en", "flags": [],
                      "releases": [f"Glass.Harbor.S0{s}E0{e}.1080p.WEB-DL.DDP5.1.H.264-NTb"],
                      "movie": {"title": "Glass Harbor", "episode_info": {"season": int(s), "episode": int(e)}},
                      "stats": {"downloads": 300}})
    if "northbound" in kw:
        items.append({"id": "nb1", "language": "en", "flags": ["hearing_impaired"],
                      "releases": ["Northbound.2025.1080p.WEB-DL.DDP5.1.H.264-FLUX"], "stats": {"downloads": 50}})
    if "red" in kw:
        items.append({"id": "rm1", "language": "en", "flags": [], "releases": ["Red.Meridian.2022.1080p.WEB-DL-GOOD"]})
    return JSONResponse({"data": items})


async def pod_download(request: Request):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hi")
        zf.writestr(f"{request.path_params['id']}.srt", SRT.encode("cp1252"))
    return Response(buf.getvalue(), media_type="application/zip")


async def state(request: Request):
    return JSONResponse({"requests": STATE["requests"][-500:], "torrents": [_info(t) for t in STATE["torrents"].values()],
                         "share_limits": STATE["share_limits"], "deleted": STATE["deleted"]})


async def unhide(request: Request):
    for r in RELEASES:
        if r["id"] == int(request.path_params["id"]):
            r["hidden"] = False
    return JSONResponse({"ok": True})


routes = [
    Route("/tvmaze/search/shows", tvm_search), Route("/tvmaze/shows/{id:int}", tvm_show),
    Route("/tvmaze/shows/{id:int}/episodes", tvm_eps), Route("/tvmaze/shows/{id:int}/akas", tvm_akas),
    Route("/tmdb/3/configuration", tmdb_config), Route("/tmdb/3/search/movie", tmdb_search),
    Route("/tmdb/3/movie/{id:int}", tmdb_movie),
    Route("/torznab/api", torznab), Route("/torznab/dl/{id:int}", torznab_dl),
    Route("/site/login.php", site_login_page), Route("/site/takelogin.php", site_takelogin, methods=["POST"]),
    Route("/site/index.php", site_index), Route("/site/browse.php", site_browse),
    Route("/site/download.php", site_download),
    Route("/cf/api/search", cf_search), Route("/fs/", fs_index), Route("/fs/v1", fs_v1, methods=["POST"]),
    Route("/blocked/{rest:path}", blocked_site),
    Route("/qb/api/v2/auth/login", qb_login, methods=["POST"]), Route("/qb/api/v2/app/version", qb_version),
    Route("/qb/api/v2/app/preferences", qb_prefs), Route("/qb/api/v2/torrents/categories", qb_categories),
    Route("/qb/api/v2/torrents/createCategory", qb_create_category, methods=["POST"]),
    Route("/qb/api/v2/torrents/add", qb_add, methods=["POST"]), Route("/qb/api/v2/torrents/info", qb_info),
    Route("/qb/api/v2/torrents/delete", qb_delete, methods=["POST"]),
    Route("/qb/api/v2/torrents/setShareLimits", qb_share, methods=["POST"]),
    Route("/os/api/v1/login", os_login, methods=["POST"]), Route("/os/api/v1/infos/user", os_user),
    Route("/os/api/v1/infos/formats", os_formats), Route("/os/api/v1/subtitles", os_search),
    Route("/os/api/v1/download", os_download, methods=["POST"]), Route("/os/file/{id}", os_file),
    Route("/pod/subtitles/search/advanced", pod_search), Route("/pod/subtitles/{id}/download", pod_download),
    Route("/_state", state), Route("/_unhide/{id:int}", unhide),
]
app = Starlette(routes=routes)

DEFINITIONS = {
    "omnitest.yml": """---
id: omnitest
name: OmniTest Private
description: "Mock private tracker used by the test-suite"
language: en-US
type: private
encoding: UTF-8
requestDelay: 1
links:
  - http://127.0.0.1:{port}/site/
caps:
  categorymappings:
    - {{id: 1, cat: TV/HD, desc: "TV HD"}}
    - {{id: 2, cat: Movies/HD, desc: "Movies HD"}}
    - {{id: 3, cat: Audio, desc: "Music"}}
  modes:
    search: [q]
    tv-search: [q, season, ep]
    movie-search: [q, imdbid]
settings:
  - name: username
    type: text
    label: Username
  - name: password
    type: password
    label: Password
  - name: sort
    type: select
    label: Sort requested from site
    default: seeders
    options:
      seeders: seeders
      added: created
login:
  path: login.php
  method: form
  form: form#login
  inputs:
    username: "{{{{ .Config.username }}}}"
    password: "{{{{ .Config.password }}}}"
    remember: 1
  error:
    - selector: div.error
  test:
    path: index.php
    selector: a[href="logout.php"]
search:
  paths:
    - path: browse.php
  inputs:
    q: "{{{{ .Keywords }}}}"
    $raw: "{{{{ range .Categories }}}}c{{{{.}}}}=1&{{{{end}}}}"
    sort: "{{{{ .Config.sort }}}}"
  keywordsfilters:
    - name: re_replace
      args: ["[^a-zA-Z0-9]+", " "]
  rows:
    selector: table.torrents > tbody > tr:has(a[href^="details.php"])
  fields:
    category:
      selector: a[href*="cat="]
      attribute: href
      filters:
        - name: querystring
          args: cat
    title:
      selector: a[href^="details.php"]
    details:
      selector: a[href^="details.php"]
      attribute: href
    download:
      selector: a[href^="download.php"]
      attribute: href
    size:
      selector: td:nth-child(4)
    seeders:
      selector: td:nth-child(5)
    leechers:
      selector: td:nth-child(6)
    date:
      selector: td:nth-child(3)
      filters:
        - name: append
          args: " +00:00"
        - name: dateparse
          args: "yyyy-MM-dd HH:mm:ss zzz"
    downloadvolumefactor:
      case:
        img.freeleech: 0
        "*": 1
    uploadvolumefactor:
      text: 1
    minimumratio:
      text: 1.0
    minimumseedtime:
      text: 172800
""",
    "cftest.yml": """---
id: cftest
name: CF Test Public
description: "Mock public JSON site behind a Cloudflare check"
language: en-US
type: public
encoding: UTF-8
links:
  - http://127.0.0.1:{port}/cf/
caps:
  categorymappings:
    - {{id: movies, cat: Movies, desc: Movies}}
    - {{id: tv, cat: TV, desc: TV}}
  modes:
    search: [q]
    tv-search: [q, season, ep]
    movie-search: [q]
settings: []
search:
  paths:
    - path: "api/search?q={{{{ .Keywords }}}}"
      response:
        type: json
  rows:
    selector: data.torrents
  fields:
    title:
      selector: name
    category:
      selector: category
    infohash:
      selector: info_hash
    size:
      selector: size
    seeders:
      selector: seeders
    leechers:
      selector: leechers
    date:
      selector: added
    downloadvolumefactor:
      text: 0
    uploadvolumefactor:
      text: 1
""",
    "blockedtest.yml": """---
id: blockedtest
name: Blocked Test
description: "Mock site blocked by the ISP"
language: en-US
type: public
links:
  - http://127.0.0.1:{port}/blocked/
caps:
  categorymappings:
    - {{id: 1, cat: TV, desc: TV}}
  modes:
    search: [q]
search:
  paths:
    - path: "search/{{{{ .Keywords }}}}"
  rows:
    selector: tr.row
  fields:
    title:
      selector: a
    download:
      selector: a
      attribute: href
""",
}


def main() -> None:
    global PORT, ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18999)
    ap.add_argument("--dir", default="/tmp/omniarr-mock")
    a = ap.parse_args()
    PORT, ROOT = a.port, Path(a.dir)
    (ROOT / "data" / "torrents").mkdir(parents=True, exist_ok=True)
    FIX.mkdir(parents=True, exist_ok=True)
    (FIX / "fixture.srt").write_text(SRT)
    import uvicorn
    uvicorn.run(app, host=os.environ.get("MOCK_BIND", "127.0.0.1"), port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
