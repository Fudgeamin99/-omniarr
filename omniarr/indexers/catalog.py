"""The list of site definitions: bundled snapshot, weekly updates, and user custom files."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path

from .. import config, log, net
from .cardigann import Definition, load_definition

L = log.get("catalog")

_summaries: dict[str, dict] | None = None
_paths: dict[str, Path] = {}
_defs: dict[str, Definition] = {}


def _dirs() -> list[Path]:
    # later dirs override earlier ones
    return [config.BUNDLED_DEFINITIONS, config.DEFINITIONS_DIR, config.CUSTOM_DEFINITIONS_DIR]


def _scan() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for d in _dirs():
        if d.is_dir():
            for p in sorted(d.glob("*.yml")):
                found[p.stem] = p
    return found


def _cache_file() -> Path:
    return config.DEFINITIONS_DIR / "_catalog.json"


def _build_sync() -> dict[str, dict]:
    paths = _scan()
    cache = {}
    try:
        cache = json.loads(_cache_file().read_text())
    except Exception:
        cache = {}
    out: dict[str, dict] = {}
    changed = False
    for did, p in paths.items():
        key = f"{p}:{p.stat().st_mtime_ns}"
        hit = cache.get(did)
        if hit and hit.get("_key") == key:
            out[did] = hit
            continue
        try:
            s = load_definition(p).summary()
        except Exception as e:  # broken file: skip it
            L.warning("Definition %s failed to load: %s", p.name, e)
            continue
        s["_key"] = key
        s["custom"] = p.parent == config.CUSTOM_DEFINITIONS_DIR
        out[did] = s
        changed = True
    if changed or len(out) != len(cache):
        try:
            _cache_file().write_text(json.dumps(out))
        except OSError:
            pass
    return out


async def summaries() -> dict[str, dict]:
    global _summaries, _paths
    if _summaries is None:
        _paths = _scan()
        _summaries = await asyncio.to_thread(_build_sync)
    return _summaries


def reset() -> None:
    global _summaries
    _summaries = None
    _defs.clear()


def get(definition_id: str) -> Definition:
    d = _defs.get(definition_id)
    if d is not None:
        return d
    paths = _paths or _scan()
    p = paths.get(definition_id)
    if p is None:
        raise KeyError(f"Unknown site definition '{definition_id}'")
    d = load_definition(p)
    _defs[definition_id] = d
    return d


async def update_from_github() -> int:
    """Download the latest Prowlarr definitions archive and refresh the local copy."""
    r = await net.client(timeout=120).get(config.DEFINITIONS_ARCHIVE_URL, follow_redirects=True)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    prefix_part = f"/definitions/{config.DEFINITIONS_VERSION}/"
    count = 0
    target = config.DEFINITIONS_DIR
    target.mkdir(parents=True, exist_ok=True)
    for name in zf.namelist():
        if prefix_part in name and name.endswith(".yml"):
            data = zf.read(name)
            (target / Path(name).name).write_bytes(data)
            count += 1
    reset()
    L.info("Updated %d site definitions", count)
    return count
