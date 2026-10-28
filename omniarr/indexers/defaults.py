"""Popular English public indexers that are added automatically on first start."""
from __future__ import annotations

import time

from .. import db, log, settings
from . import catalog

L = log.get("indexers")

# (definition id, use for, priority — lower wins ties, Cloudflare bypass on from the start)
RECOMMENDED: list[tuple[str, str, int, bool]] = [
    ("1337x", "both", 20, True),
    ("eztv", "tv", 20, True),
    ("yts", "movies", 20, False),
    ("thepiratebay", "both", 25, False),
    ("limetorrents", "both", 30, False),
    ("torrentdownloads", "both", 35, False),
    ("uindex", "both", 35, True),
    ("nyaasi", "tv", 30, False),     # anime (English dubs / dual audio)
]


def add_recommended() -> list[str]:
    """Add any recommended public indexers that aren't already set up. Returns the names added."""
    have = {r["definition_id"] for r in db.rows("SELECT definition_id FROM indexers WHERE kind = 'cardigann'")}
    added = []
    for def_id, use_for, priority, flaresolverr in RECOMMENDED:
        if def_id in have:
            continue
        try:
            d = catalog.get(def_id)
        except KeyError:
            continue
        if d.unsupported_reason():
            continue
        db.insert("indexers", {
            "name": d.name, "kind": "cardigann", "definition_id": def_id, "enabled": 1, "priority": priority,
            "use_for": use_for, "settings": db.jdump({"base_url": d.links[0] if d.links else ""}),
            "flaresolverr": 1 if flaresolverr else 0, "use_proxy": 0, "seed_goals": 1, "added_at": time.time(),
        })
        added.append(d.name)
    if added:
        L.info("Added public indexers: %s", ", ".join(added))
    return added


def seed_on_first_start() -> None:
    import os
    if os.environ.get("OMNIARR_NO_DEFAULT_INDEXERS") == "1":
        return
    gen = settings.get("general")
    if gen.get("defaults_seeded"):
        return
    if not db.scalar("SELECT COUNT(*) FROM indexers"):
        add_recommended()
    settings.save("general", {"defaults_seeded": True})
