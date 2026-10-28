"""Background tasks on timers: downloads, RSS, metadata refresh, subtitles, housekeeping, definition updates."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Awaitable, Callable

from . import config, db, downloads, library, log, search
from .indexers import catalog
from .subtitles import manager as subs

L = log.get("scheduler")


async def _refresh_all() -> str:
    n = 0
    for s in db.rows("SELECT id, title, status FROM series"):
        try:
            await library.refresh_series(s["id"])
            library.scan_series(s["id"])
            n += 1
        except Exception as e:
            L.warning("Refresh failed for %s: %s", s["title"], e)
    for m in db.rows("SELECT id, title FROM movies"):
        try:
            await library.refresh_movie(m["id"])
            library.scan_movie(m["id"])
            n += 1
        except Exception as e:
            L.warning("Refresh failed for %s: %s", m["title"], e)
    return f"Refreshed {n} item(s)"


async def _housekeeping() -> str:
    cutoff = time.time() - 365 * 86400
    db.execute("DELETE FROM history WHERE at < ?", cutoff)
    db.execute("DELETE FROM downloads WHERE status IN ('imported','failed','removed') AND finished_at < ? "
               "AND removed_from_client = 1", time.time() - 90 * 86400)
    db.execute("PRAGMA optimize")
    return "Tidied up"


async def _update_definitions() -> str:
    try:
        n = await catalog.update_from_github()
        return f"Updated {n} site definitions"
    except Exception as e:
        return f"Couldn't update site definitions ({e.__class__.__name__}) — using the built-in copy"


TASKS: dict[str, dict] = {
    "downloads": {"label": "Check downloads", "fn": downloads.check},
    "rss": {"label": "Check indexers for new releases", "fn": search.rss_sync},
    "refresh": {"label": "Refresh show & movie info", "fn": _refresh_all},
    "subtitles": {"label": "Find missing subtitles", "fn": subs.sweep},
    "housekeeping": {"label": "Housekeeping", "fn": _housekeeping},
    "definitions": {"label": "Update site definitions", "fn": _update_definitions},
}
_state: dict[str, dict] = {k: {"last_run": None, "last_result": "", "running": False, "next_run": None}
                           for k in TASKS}
_loop_task: asyncio.Task | None = None


async def run(name: str) -> str:
    st = _state[name]
    if st["running"]:
        return "Already running"
    st["running"] = True
    t0 = time.time()
    try:
        fn: Callable[[], Awaitable[str]] = TASKS[name]["fn"]
        res = await fn()
        st["last_result"] = res or "Done"
    except Exception as e:
        L.exception("Task %s failed", name)
        st["last_result"] = f"Failed: {e}"
    finally:
        st["running"] = False
        st["last_run"] = time.time()
        st["duration"] = round(time.time() - t0, 1)
        st["next_run"] = time.time() + config.INTERVALS[name]
    return st["last_result"]


async def _loop() -> None:
    # stagger the first runs so start-up isn't a burst of traffic
    first = {"downloads": 5, "rss": 90, "refresh": 600, "subtitles": 300, "housekeeping": 3600,
             "definitions": 1800}
    now = time.time()
    scale = float(os.environ.get("OMNIARR_FIRST_RUN_SCALE", "1"))
    for k in TASKS:
        _state[k]["next_run"] = now + min(first.get(k, 60) * scale, config.INTERVALS[k])
    while True:
        now = time.time()
        for k in TASKS:
            st = _state[k]
            if not st["running"] and st["next_run"] and now >= st["next_run"]:
                asyncio.create_task(run(k))
                st["next_run"] = now + config.INTERVALS[k]
        await asyncio.sleep(1)


def start() -> None:
    global _loop_task
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(_loop())


async def stop() -> None:
    if _loop_task:
        _loop_task.cancel()


def status() -> list[dict]:
    return [{"name": k, "label": TASKS[k]["label"], "interval": config.INTERVALS[k], **_state[k]} for k in TASKS]
