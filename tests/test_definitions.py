"""Run real Prowlarr definitions through the engine against fixture pages shaped like the real sites."""
import json
import time
from pathlib import Path

import httpx2 as httpx

from omniarr import config, db, net
from omniarr.indexers import catalog
from omniarr.indexers.cardigann import CardigannIndexer, load_definition
from omniarr.net import Session

DEFS = Path(__file__).resolve().parent.parent / "definitions"
H1 = "a" * 40

YTS_JSON = {"status": "ok", "data": {"movie_count": 1, "movies": [{
    "title": "Northbound", "title_long": "Northbound (2025)", "year": 2025, "url": "https://yts.mx/movies/northbound-2025",
    "imdb_code": "tt2000101", "large_cover_image": "https://yts.mx/x.jpg",
    "torrents": [
        {"url": "https://yts.mx/torrent/download/AAA", "hash": H1.upper(), "quality": "1080p", "type": "web",
         "video_codec": "x264", "bit_depth": "8", "audio_channels": "5.1", "seeds": 210, "peers": 30,
         "size_bytes": 2147483648, "date_uploaded_unix": 1735689600},
        {"url": "https://yts.mx/torrent/download/BBB", "hash": "b" * 40, "quality": "2160p", "type": "bluray",
         "video_codec": "x265", "bit_depth": "10", "audio_channels": "2.0", "seeds": 50, "peers": 3,
         "size_bytes": 5368709120, "date_uploaded_unix": 1735689600}]}]}}

EZTV_HTML = """<html><body><table class="forum_header_border"><tr><td>Releases</td></tr>
<tr name="hover" class="forum_header_border"><td></td>
<td><a href="/ep/1/glass-harbor-s02e01/" title="Glass Harbor S02E01 1080p WEB H264-NTb [eztv] (1.85 GB)" class="epinfo">x</a></td>
<td><a href="magnet:?xt=urn:btih:%s&dn=x" class="magnet">m</a></td><td>1.85 GB</td><td>3h</td><td>1,204</td></tr>
<tr name="hover" class="forum_header_border"><td></td>
<td><a href="/ep/2/other/" title="Other Show S01E01 720p HDTV x264 [eztv]" class="epinfo">x</a></td>
<td><a href="magnet:?xt=urn:btih:%s&dn=y" class="magnet">m</a></td><td>500 MB</td><td>1d</td><td>12</td></tr>
</table></body></html>""" % ("c" * 40, "d" * 40)

TPB_JSON = [{"id": "77", "name": "Glass.Harbor.S02E01.1080p.WEB.H264-NTb", "info_hash": "E" * 40, "leechers": "4",
             "seeders": "321", "num_files": "1", "size": "1987654321", "username": "uploader1", "added": "1735689600",
             "status": "vip", "category": "208", "imdb": "tt1000001"}]

X1337_SEARCH = """<html><body><table><tbody>
<tr><td class="coll-1 name"><a href="/sub/41/0/" class="icon"></a><a href="/torrent/555/Glass-Harbor-S02E01-1080p-WEB-H264-NTb/">Glass.Harbor.S02E01.1080p.WEB.H264-NTb</a></td>
<td class="coll-2 seeds">1,050</td><td class="coll-3 leeches">40</td><td class="coll-date">5am Oct. 3rd</td>
<td class="coll-4 size mob-uploader">1.9 GB<span class="seeds">1,050</span></td><td class="coll-5 uploader"><a href="#">EZTV</a></td></tr>
</tbody></table></body></html>"""
X1337_DETAILS = """<html><body><ul><li><a href="magnet:?xt=urn:btih:%s&dn=Glass">Magnet Download</a></li>
<li><a href="http://itorrents.org/torrent/X.torrent">Torrent Download</a></li></ul></body></html>""" % ("f" * 40)


def _handler(request: httpx.Request) -> httpx.Response:
    u = str(request.url)
    if "list_movies.json" in u:
        assert "query_term=tt2000101" in u, u                 # IMDb id preferred over keywords
        return httpx.Response(200, json=YTS_JSON)
    if "eztv" in request.url.host and "/search/" in u:
        assert "glass-harbor" in u.lower(), u                 # keyword filters applied
        return httpx.Response(200, text=EZTV_HTML, headers={"content-type": "text/html"})
    if "apibay" in request.url.host:
        return httpx.Response(200, json=TPB_JSON)
    if "1337x" in request.url.host and "/torrent/555/" in u:
        return httpx.Response(200, text=X1337_DETAILS, headers={"content-type": "text/html"})
    if "1337x" in request.url.host:
        if "/search/" in u or "sort-search" in u:
            return httpx.Response(200, text=X1337_SEARCH, headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
    return httpx.Response(404, text="nope")


def _setup(tmp="/tmp/omniarr-defs-test"):
    config.CONFIG_DIR = Path(tmp)
    config.DB_PATH = Path(tmp) / "t.db"
    config.LOG_DIR = Path(tmp) / "logs"
    config.DEFINITIONS_DIR = Path(tmp) / "defs"
    config.CUSTOM_DEFINITIONS_DIR = Path(tmp) / "custom"
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.close()
    db.connect()
    net._clients.clear()
    transport = httpx.MockTransport(_handler)
    orig = net.client

    def fake_client(proxy=None, timeout=30.0):
        return httpx.AsyncClient(transport=transport, follow_redirects=False)
    net.client = fake_client
    return orig


def _idx(def_id: str, cfg: dict | None = None) -> CardigannIndexer:
    d = load_definition(DEFS / f"{def_id}.yml")
    s = Session(key=("t", def_id), name=def_id)
    return CardigannIndexer(1, d.name, d, cfg or {}, s)


async def test_yts_json_with_parent_fields():
    orig = _setup()
    try:
        idx = _idx("yts")
        q = idx.build_query("movies", "Northbound 2025", imdb_id="tt2000101", year=2025)
        rels = await idx.search(q)
        assert len(rels) == 2, rels
        r = rels[0]
        assert r.title.startswith("Northbound (2025) 1080p WEBRip 5.1 x264"), r.title
        assert r.imdb_id == "tt2000101" and r.seeders == 210 and r.size == 2147483648
        assert r.categories == [2040] and r.info_hash == H1
        assert "10Bit" in rels[1].title and rels[1].categories == [2045]
    finally:
        net.client = orig


async def test_eztv_html_keywords_and_headers():
    orig = _setup()
    try:
        idx = _idx("eztv")
        rels = await idx.search(idx.build_query("tv", "Glass Harbor S02E01", season=2, ep=1))
        assert len(rels) == 1, [r.title for r in rels]          # andmatch filter drops "Other Show"
        r = rels[0]
        assert r.title == "Glass Harbor S02E01 1080p WEB H264-NTb", r.title
        assert r.magnet and r.magnet.startswith("magnet:") and r.seeders == 1204
        assert r.size and 1.8e9 < r.size < 2.1e9
        assert r.published and r.published < time.time() - 3 * 3600 + 60
    finally:
        net.client = orig


async def test_tpb_json_root_rows_and_infohash_magnet():
    orig = _setup()
    try:
        idx = _idx("thepiratebay")
        rels = await idx.search(idx.build_query("tv", "Glass Harbor S02E01", season=2, ep=1))
        assert len(rels) == 1
        r = rels[0]
        assert r.magnet and ("e" * 40) in r.magnet and r.seeders == 321 and r.size == 1987654321
        assert r.imdb_id == "tt1000001" and r.categories == [5040]
    finally:
        net.client = orig


async def test_1337x_details_page_download():
    orig = _setup()
    try:
        idx = _idx("1337x", {"sort": "time", "type": "desc", "primarydownloadlink": "magnet:",
                             "fallbackdownloadlink": "//itorrents."})
        rels = await idx.search(idx.build_query("tv", "Glass Harbor S02E01"))
        assert rels, "no rows parsed"
        r = rels[0]
        assert r.title == "Glass.Harbor.S02E01.1080p.WEB.H264-NTb", r.title
        assert r.seeders == 1050 and r.leechers == 40 and r.categories == [5040]
        data, magnet = await idx.fetch_torrent(r)
        assert magnet and ("f" * 40) in magnet, magnet
    finally:
        net.client = orig


async def test_every_definition_builds_search_requests():
    """Render every usable definition's search URLs/inputs for TV and movie queries without errors."""
    from omniarr.indexers.template import render
    bad = []
    for p in sorted(DEFS.glob("*.yml")):
        d = load_definition(p)
        if d.unsupported_reason():
            continue
        idx = CardigannIndexer(1, d.name, d, {}, Session(key=("t", d.id), name=d.id))
        for kind in d.kinds():
            q = idx.build_query(kind, "Glass Harbor S02E01" if kind == "tv" else "Northbound 2025", season=2, ep=1,
                                imdb_id="tt2000101", year=2025)
            ctx = idx._ctx(q)
            try:
                ctx["Keywords"] = idx._apply_keyword_filters(q["keywords"], ctx)
                for pdef in d.search.get("paths") or [{"path": d.search.get("path", "")}]:
                    idx.url(pdef.get("path") or "", ctx)
                    for v in {**(d.search.get("inputs") or {}), **(pdef.get("inputs") or {})}.values():
                        render(str(v), ctx)
            except Exception as e:
                bad.append(f"{d.id}: {e}")
    assert not bad, bad[:10]
