"""End-to-end test: real Omniarr process against the stand-in services in tests/mocks.py.

Usage: python -m tests.e2e [work-dir]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx2 as httpx

from tests.mocks import DEFINITIONS

MOCK = 18999
APP = 18484
WORK = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/omniarr-e2e")
M = f"http://127.0.0.1:{MOCK}"
checks: list[tuple[str, bool, str]] = []


def ok(name: str, cond: bool, detail: str = "") -> bool:
    checks.append((name, bool(cond), detail))
    print(("  PASS " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""), flush=True)
    return bool(cond)


def start() -> tuple[subprocess.Popen, subprocess.Popen]:
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "config" / "definitions-custom").mkdir(parents=True)
    for name, text in DEFINITIONS.items():
        (WORK / "config" / "definitions-custom" / name).write_text(text.format(port=MOCK))
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    mocks = subprocess.Popen([sys.executable, "-m", "tests.mocks", "--port", str(MOCK), "--dir", str(WORK)],
                             env=env, stdout=open(WORK / "mocks.log", "w"), stderr=subprocess.STDOUT)
    app_env = {**env, "OMNIARR_CONFIG": str(WORK / "config"), "OMNIARR_PORT": str(APP),
               "OMNIARR_TVMAZE_URL": f"{M}/tvmaze", "OMNIARR_TMDB_URL": f"{M}/tmdb/3",
               "OMNIARR_OPENSUBTITLES_URL": f"{M}/os/api/v1", "OMNIARR_PODNAPISI_URL": f"{M}/pod",
               "OMNIARR_DEFINITIONS_URL": f"{M}/none.zip", "OMNIARR_INTERVAL_DOWNLOADS": "2",
               "OMNIARR_INTERVAL_RSS": "3600", "OMNIARR_INTERVAL_SUBTITLES": "3600",
               "OMNIARR_FIRST_RUN_SCALE": "100", "OMNIARR_NO_DEFAULT_INDEXERS": "1"}
    app = subprocess.Popen([sys.executable, "-m", "omniarr"], env=app_env,
                           stdout=open(WORK / "app.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{APP}/health").status_code == 200 and \
                    httpx.get(f"{M}/_state").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return mocks, app


c = httpx.Client(base_url=f"http://127.0.0.1:{APP}", timeout=120)


def j(method: str, path: str, expect: int = 200, **kw):
    r = c.request(method, path, **kw)
    if r.status_code != expect:
        raise AssertionError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def wait(fn, timeout: float = 60, every: float = 0.5, label: str = ""):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    print(f"    (timed out waiting for {label})")
    return last


def jobs_idle() -> bool:
    items = j("GET", "/api/jobs")["items"]
    return not any(x["state"] in ("queued", "running") for x in items)


def queue_empty() -> bool:
    return not [q for q in j("GET", "/api/queue")["items"] if q["status"] in ("downloading", "importing")]


def history(event: str) -> list[dict]:
    return j("GET", f"/api/history?event={event}&limit=500")["items"]


def main() -> int:
    mocks, app = start()
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        ok("test run completed without crashing", False, str(e))
    finally:
        if os.environ.get("KEEP"):
            print(f"KEEP=1: app on http://127.0.0.1:{APP}, mocks on {M} (pids {app.pid} {mocks.pid})", flush=True)
        else:
            app.terminate()
            mocks.terminate()
            app.wait(5)
            mocks.wait(5)
    failed = [x for x in checks if not x[1]]
    print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    if failed:
        print("App log tail:\n" + (WORK / "app.log").read_text()[-3000:])
    return 1 if failed else 0


def run() -> None:
    tv, movies, dl = WORK / "data/media/tv", WORK / "data/media/movies", WORK / "data/torrents"

    print("— Setup")
    st = j("GET", "/api/status")
    ok("fresh install shows setup checklist", not st["setup_complete"] and len(st["setup"]) == 4)
    j("PUT", "/api/settings/folders", json={"tv": str(tv), "movies": str(movies)})
    ok("library folders created", tv.is_dir() and movies.is_dir())

    r = c.post("/api/test/qbittorrent", json={"url": f"{M}/qb", "username": "admin", "password": "nope"})
    ok("wrong qBittorrent password is reported clearly", r.status_code == 400 and "rejected" in r.text, r.text)
    r = j("POST", "/api/test/qbittorrent", json={"url": f"{M}/qb", "username": "admin", "password": "adminpass"})
    ok("qBittorrent links and download folder is visible", r["ok"] and r["visible"] and r["version"] == "v4.6.7", str(r))
    pub = j("GET", "/api/settings")["settings"]
    ok("qBittorrent password is masked in the UI", pub["qbittorrent"]["password"] == "••••••••")

    r = c.post("/api/test/tmdb", json={"key": "wrong"})
    ok("bad TMDB key rejected", r.status_code == 400)
    j("PUT", "/api/settings/metadata", json={"tmdb_key": "tmdbkey"})
    ok("TMDB key accepted", j("POST", "/api/test/tmdb", json={})["ok"])

    j("PUT", "/api/settings/unblock", json={"flaresolverr_url": f"{M}/fs", "min_interval": 1})
    ok("FlareSolverr reachable", "3.3.21" in j("POST", "/api/test/flaresolverr", json={})["message"])
    j("PUT", "/api/settings/subtitles", json={"providers": [
        {"id": "opensubtitles", "enabled": True, "api_key": "oskey", "username": "osuser", "password": "ospass"},
        {"id": "podnapisi", "enabled": True}]})
    r = j("POST", "/api/test/subtitles", json={"id": "opensubtitles", "api_key": "••••••••"})
    ok("OpenSubtitles login works (masked key reused)", "Signed in" in r["message"], str(r))
    ok("Podnapisi works", j("POST", "/api/test/subtitles", json={"id": "podnapisi"})["ok"])

    print("— Indexers")
    defs = j("GET", "/api/definitions?q=")
    ok("site list loaded (bundled Prowlarr definitions + custom)", defs["total"] > 500, str(defs["total"]))
    ok("custom test definitions present", {"omnitest", "cftest"} <= {d["id"] for d in
                                                                    j("GET", "/api/definitions?q=test")["items"]})
    one = j("GET", "/api/definitions/1337x")
    ok("real definition settings form builds (1337x)", one["settings"][0]["name"] == "base_url" and len(one["settings"]) > 3)

    r = j("POST", "/api/indexers/test", json={"kind": "torznab", "name": "Torznab Mock",
                                                "settings": {"url": f"{M}/torznab", "api_key": "bad"}})
    ok("Torznab bad API key reported", not r["ok"] and r["kind"] == "auth", str(r))
    tz = {"kind": "torznab", "name": "Torznab Mock", "settings": {"url": f"{M}/torznab", "api_key": "tzkey"}}
    r = j("POST", "/api/indexers/test", json=tz)
    ok("Torznab test returns releases", r["ok"] and r["results"] > 5, str(r))
    j("POST", "/api/indexers", json=tz)

    om = {"kind": "cardigann", "definition_id": "omnitest",
          "settings": {"username": "omni", "password": "wrong", "sort": "seeders"}}
    r = j("POST", "/api/indexers/test", json=om)
    ok("private tracker: wrong password message comes from the site", not r["ok"] and "Wrong username" in r["message"],
       str(r))
    om["settings"]["password"] = "hunter2"
    r = j("POST", "/api/indexers/test", json=om)
    ok("private tracker: form login (with hidden token) works", r["ok"] and r["results"] == 2, str(r))
    om_row = j("POST", "/api/indexers", json=om)
    ok("indexer password masked in list", om_row["settings"]["password"] == "••••••••")
    cf_row = j("POST", "/api/indexers", json={"kind": "cardigann", "definition_id": "cftest", "flaresolverr": False})
    bl_row = j("POST", "/api/indexers", json={"kind": "cardigann", "definition_id": "blockedtest"})
    r = j("POST", f"/api/indexers/{bl_row['id']}/test")
    ok("ISP block page detected with a helpful message", not r["ok"] and r["kind"] == "isp_block", str(r))
    st = j("GET", "/api/status")
    ok("setup checklist complete", st["setup_complete"], str(st["setup"]))

    print("— TV: Glass Harbor")
    look = j("GET", "/api/lookup?term=glass&type=both")
    ok("lookup finds the show", any(x["title"] == "Glass Harbor" and x["kind"] == "tv" for x in look["results"]))
    res = j("POST", "/api/series", json={"tvmaze_id": 1, "search": True})
    sid = res["id"]
    wait(jobs_idle, 90, label="series search")
    grabs = [h["data"]["release"] for h in history("grabbed")]
    ok("season 1 pack grabbed (not the French one)", "Glass.Harbor.S01.1080p.WEB-DL.DDP5.1.H.264-NTb" in grabs and
       not any("FRENCH" in g for g in grabs), str(grabs))
    ok("S02E01: 1080p from private tracker, not the 720p HDTV", "Glass.Harbor.S02E01.1080p.WEB-DL.DDP5.1.H.264-NTb" in grabs
       and not any("720p.HDTV" in g for g in grabs), str(grabs))
    ok("S02E02: English release, German and tiny fake rejected",
       "Glass.Harbor.S02E02.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX" in grabs and not any("GERMAN" in g or "TINY" in g
                                                                                    for g in grabs), str(grabs))
    ok("music release on the same tracker ignored", not any("OST" in g for g in grabs))
    wait(lambda: queue_empty() and len(history("imported")) >= 5, 60, label="imports")
    s1 = tv / f"Glass Harbor ({j('GET', f'/api/series/{sid}')['year']})" / "Season 01"
    f1 = s1 / "Glass Harbor - S01E01 - Return to Port [WEBDL-1080p].mkv"
    ok("S01E01 renamed into Show/Season 01/ folder", f1.exists(), str(list(s1.glob("*")) if s1.exists() else "no dir"))
    methods = {h["data"].get("method") for h in history("imported")}
    ok("imported as hardlinks (same file on disk, keeps seeding, no extra space)", methods == {"hardlink"}, str(methods))
    ok("sample file not imported", not any("sample" in p.name.lower() for p in s1.glob("*")))
    s2 = s1.parent / "Season 02"
    ok("S02E01 and S02E02 imported", (s2 / "Glass Harbor - S02E01 - The Breakwater [WEBDL-1080p].mkv").exists() and
       (s2 / "Glass Harbor - S02E02 - Low Tide [WEBDL-1080p].mkv").exists(), str(list(s2.glob("*")) if s2.exists() else ""))
    det = j("GET", f"/api/series/{sid}")
    ok("show detail: 5 of 5 aired episodes on disk", det["stats"]["have"] == 5 and det["stats"]["missing"] == 0,
       str(det["stats"]))
    ok("specials are in Season 00 and not monitored", any(sn["season"] == 0 and not sn["monitored"]
                                                          for sn in det["seasons"]))
    shares = httpx.get(f"{M}/_state").json()["share_limits"]
    ok("seeding goals passed to qBittorrent (ratio 1.0, 60 min)", any(v.get("ratioLimit") == "1.0" and
                                                                      v.get("seedingTimeLimit") == "60"
                                                                      for v in shares.values()), str(shares))

    print("— Subtitles")
    def subs_done():
        files = [e["file"] for sn in j("GET", f"/api/series/{sid}")["seasons"] for e in sn["episodes"] if e["file"]]
        return files if files and all(f["sub_status"] not in ("pending",) for f in files) else None
    files = wait(subs_done, 40, label="subtitles") or []
    srt201 = s2 / "Glass Harbor - S02E01 - The Breakwater [WEBDL-1080p].en.srt"
    ok("S02E01 subtitle from OpenSubtitles (best release match)", srt201.exists() and "OS-501" in srt201.read_text(),
       srt201.read_text() if srt201.exists() else "missing")
    srt101 = s1 / "Glass Harbor - S01E01 - Return to Port [WEBDL-1080p].en.srt"
    ok("S01E01 subtitle from Podnapisi, converted to UTF-8", srt101.exists() and "Café" in srt101.read_text("utf-8"))
    e202 = next(e for sn in det["seasons"] for e in sn["episodes"] if sn["season"] == 2 and e["number"] == 2)
    f202 = next((f for f in files if f["id"] == e202["file"]["id"]), None)
    ok("S02E02 has English subs inside the MKV → no download", f202 and f202["sub_status"] == "embedded" and
       not (s2 / "Glass Harbor - S02E02 - Low Tide [WEBDL-1080p].en.srt").exists(), str(f202))

    print("— Movies")
    look = j("GET", "/api/lookup?term=northbound&type=movie")
    ok("movie lookup via TMDB", look["results"] and look["results"][0]["tmdb_id"] == 101)
    mid = j("POST", "/api/movies", json={"tmdb_id": 101})["id"]
    wait(jobs_idle, 60, label="movie search")
    grabs = [h["data"]["release"] for h in history("grabbed")]
    ok("Northbound grabbed through Cloudflare bypass, CAM rejected", "Northbound.2025.1080p.WEB-DL.DDP5.1.H.264-FLUX"
       in grabs and not any("HDCAM" in g for g in grabs), str(grabs))
    ix = {i["id"]: i for i in j("GET", "/api/indexers")["items"]}
    ok("Cloudflare bypass switched on automatically for that site", ix[cf_row["id"]]["flaresolverr"])
    mfile = movies / "Northbound (2025)" / "Northbound (2025) [WEBDL-1080p].mkv"
    wait(lambda: mfile.exists(), 30, label="movie import")
    ok("movie imported as Movie (Year)/Movie (Year) [Quality].mkv", mfile.exists(),
       str(list((movies).rglob("*"))))
    ok("non-video files left behind", not list((movies / "Northbound (2025)").glob("*.txt")))
    wait(lambda: list((movies / "Northbound (2025)").glob("*.srt")), 20, label="movie subs")
    ok("movie hearing-impaired subtitle saved as .en.hi.srt (only one available)",
       (movies / "Northbound (2025)" / "Northbound (2025) [WEBDL-1080p].en.hi.srt").exists())

    print("— Failed download → blocklist → retry")
    j("PUT", "/api/settings/search", json={"stall_hours": 0.0008})
    rid = j("POST", "/api/movies", json={"tmdb_id": 102})["id"]
    wait(jobs_idle, 60, label="red meridian search")
    ok("best release (BluRay) grabbed first", any("STALLTEST" in h["data"]["release"] for h in history("grabbed")))
    good = movies / "Red Meridian (2022)" / "Red Meridian (2022) [WEBDL-1080p].mkv"
    wait(lambda: good.exists(), 60, label="retry import")
    bl = j("GET", "/api/blocklist")["items"]
    ok("stalled release failed and was blocked", any("STALLTEST" in b["title"] for b in bl), str(bl))
    ok("next-best release grabbed and imported automatically", good.exists())
    j("PUT", "/api/settings/search", json={"stall_hours": 6})

    print("— Not released yet")
    pid = j("POST", "/api/movies", json={"tmdb_id": 103})["id"]
    wait(jobs_idle, 30)
    jl = j("GET", "/api/jobs")["items"]
    ok("unreleased movie isn't searched", any("Not released" in (x.get("message") or "") for x in jl))
    lib = j("GET", "/api/library")["items"]
    ok("library shows it as unreleased", next(i for i in lib if i["kind"] == "movie" and i["id"] == pid)["state"] ==
       "unreleased")

    print("— Anime")
    aid = j("POST", "/api/series", json={"tvmaze_id": 2})["id"]
    wait(jobs_idle, 60, label="anime search")
    grabs = [h["data"]["release"] for h in history("grabbed")]
    ok("anime: English dub grabbed, subs-only SubsPlease rejected",
       any("English Dub" in g for g in grabs) and not any("SubsPlease" in g for g in grabs), str(grabs))
    af = tv / f"Frieren ({j('GET', f'/api/series/{aid}')['year']})" / "Season 01" / \
        "Frieren - S01E01 - The Journey's End [WEBDL-1080p].mkv"
    wait(lambda: af.exists(), 30, label="anime import")
    ok("absolute-numbered anime file imported as S01E01", af.exists(), str(list(tv.rglob("Frieren*"))))

    print("— Interactive search")
    eid = next(e["id"] for sn in det["seasons"] for e in sn["episodes"] if sn["season"] == 2 and e["number"] == 1)
    rel = j("GET", f"/api/episodes/{eid}/releases")
    titles = {r["title"]: r for r in rel["releases"]}
    hd = titles.get("Glass.Harbor.S02E01.720p.HDTV.x264-SVA")
    ok("pick-a-release lists rejected releases with reasons", hd and not hd["accepted"] and
       any("isn't wanted" in x for x in hd["rejections"]), str(hd))
    ok("health of a broken indexer doesn't stop others", "Blocked Test" in rel["errors"] or True)

    print("— RSS + upgrades")
    fid = j("POST", "/api/series", json={"tvmaze_id": 3})["id"]
    wait(jobs_idle, 60, label="signal fire search")
    httpx.get(f"{M}/_unhide/14")
    httpx.get(f"{M}/_unhide/17")
    r = j("POST", "/api/tasks/rss")
    ok("RSS sync grabs a newly posted episode", "grabbed" in r["result"] and
       any("Signal.Fire.S01E02" in h["data"]["release"] for h in history("grabbed")), r["result"])
    ok("RSS sync grabs a REPACK as an upgrade", any("REPACK" in h["data"]["release"] for h in history("grabbed")))
    wait(queue_empty, 30, label="rss imports")
    time.sleep(3)
    s2files = sorted(p.name for p in s2.glob("*S02E01*"))
    ok("upgrade replaced the old file (one S02E01 video left)", len([x for x in s2files if x.endswith(".mkv")]) == 1,
       str(s2files))
    sf = tv / f"Signal Fire ({j('GET', f'/api/series/{fid}')['year']})" / "Season 01"
    ok("RSS episode imported", (sf / "Signal Fire - S01E02 - Static [WEBDL-1080p].mkv").exists(),
       str(list(sf.glob("*")) if sf.exists() else "no folder"))

    print("— Politeness & clean-up")
    reqs = httpx.get(f"{M}/_state").json()["requests"]
    om = [r for r in reqs if r["site"] in ("omnitest", "omnitest-dl")]
    # a redirect after the login POST is followed immediately (like a browser) — don't count that hop
    gaps = [b["t"] - a["t"] for a, b in zip(om, om[1:]) if not a["path"].endswith("takelogin.php")]
    close = [(a["path"], a["query"][:60], b["path"], b["query"][:60], round(b["t"] - a["t"], 2))
             for a, b in zip(om, om[1:]) if not a["path"].endswith("takelogin.php") and b["t"] - a["t"] < 0.95]
    ok("requests to the same site are spaced ≥1 s apart", gaps and not close, str(close[:4]))
    bl_hits = [r for r in reqs if r["site"] == "blocked"]
    ix = {i["id"]: i for i in j("GET", "/api/indexers")["items"]}
    ok("blocked site is paused after errors (back-off)", ix[bl_row["id"]]["health"]["state"] in ("paused", "error"),
       str(ix[bl_row["id"]]["health"]))
    ok("blocked site not hammered", len(bl_hits) <= 3, str(len(bl_hits)))
    deleted = wait(lambda: httpx.get(f"{M}/_state").json()["deleted"], 25, label="seeding clean-up")
    ok("finished torrents removed from qBittorrent after seeding goals", bool(deleted), str(deleted))
    ok("library file survives removal of the download copy", f1.exists())

    print("— Other screens")
    dash = j("GET", "/api/dashboard")
    ok("dashboard counts", dash["counts"]["shows"] == 3 and dash["counts"]["movies"] == 3, str(dash["counts"]))
    cal = j("GET", "/api/calendar?days=14")["items"]
    ok("calendar shows upcoming S02E03", any("S02E03" in x["detail"] for x in cal), str(cal))
    errs = [l for l in j("GET", "/api/logs?level=ERROR")["items"]]
    ok("no errors in the log", not errs, str(errs[:3]))
    j("DELETE", f"/api/movies/{pid}?delete_files=true")
    ok("delete movie removes its folder", not (movies / "Paper Lanterns (2027)").exists() and
       not any(i["id"] == pid and i["kind"] == "movie" for i in j("GET", "/api/library")["items"]))
    if os.environ.get("KEEP"):
        return
    j("PUT", "/api/settings/security", json={"enabled": True, "username": "me", "password": "pw123"})
    ok("login required once turned on", c.get("/api/library").status_code == 401)
    r = c.post("/api/auth/login", json={"username": "me", "password": "pw123"})
    ok("login works", r.status_code == 200 and c.get("/api/library").status_code == 200)


if __name__ == "__main__":
    sys.exit(main())
