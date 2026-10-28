"""The recommended public indexers get added on first start."""
from pathlib import Path

from omniarr import config, db, settings


def test_recommended_public_indexers_seeded_once():
    tmp = Path("/tmp/omniarr-defaults-test")
    config.CONFIG_DIR, config.DB_PATH = tmp, tmp / "t.db"
    config.LOG_DIR, config.DEFINITIONS_DIR, config.CUSTOM_DEFINITIONS_DIR = tmp / "l", tmp / "d", tmp / "c"
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.close()
    db.connect()
    from omniarr.indexers import catalog, defaults
    catalog.reset()
    defaults.seed_on_first_start()
    rows = db.rows("SELECT * FROM indexers ORDER BY priority")
    ids = {r["definition_id"] for r in rows}
    assert {"1337x", "eztv", "yts", "thepiratebay"} <= ids, ids
    assert len(rows) == len(defaults.RECOMMENDED)
    assert next(r for r in rows if r["definition_id"] == "yts")["use_for"] == "movies"
    assert next(r for r in rows if r["definition_id"] == "1337x")["flaresolverr"] == 1
    # deleting one and restarting must not re-add it; the button re-adds only missing ones
    db.execute("DELETE FROM indexers WHERE definition_id = 'yts'")
    defaults.seed_on_first_start()
    assert not db.row("SELECT 1 FROM indexers WHERE definition_id = 'yts'")
    assert defaults.add_recommended() == ["YTS"]
    assert defaults.add_recommended() == []
    assert settings.get("general")["defaults_seeded"]
