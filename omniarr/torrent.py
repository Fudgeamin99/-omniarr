"""Bencode decoding (just enough for info-hashes) and magnet helpers."""
from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import parse_qs, quote, urlparse


class BencodeError(ValueError):
    pass


def _decode(data: bytes, i: int):
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _decode(data, i)
            start = i
            v, i = _decode(data, i)
            out[k] = v
            if k == b"info":
                out[b"__info_span__"] = (start, i)
        return out, i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        n = int(data[i:colon])
        return data[colon + 1:colon + 1 + n], colon + 1 + n
    raise BencodeError(f"bad token at {i}")


def decode(data: bytes):
    try:
        v, _ = _decode(data, 0)
        return v
    except (ValueError, IndexError) as e:
        raise BencodeError(str(e)) from e


def is_torrent(data: bytes) -> bool:
    return data[:1] == b"d" and b"4:info" in data[:4096 * 4]


def info_hash(data: bytes) -> str:
    meta = decode(data)
    if not isinstance(meta, dict) or b"__info_span__" not in meta:
        raise BencodeError("no info dictionary")
    a, b = meta[b"__info_span__"]
    return hashlib.sha1(data[a:b]).hexdigest()


def torrent_name(data: bytes) -> str | None:
    meta = decode(data)
    info = meta.get(b"info") if isinstance(meta, dict) else None
    name = info.get(b"name") if isinstance(info, dict) else None
    return name.decode("utf-8", "replace") if isinstance(name, bytes) else None


def magnet_hash(magnet: str) -> str | None:
    q = parse_qs(urlparse(magnet).query)
    for xt in q.get("xt", []):
        m = re.match(r"urn:btih:([A-Za-z0-9]+)", xt)
        if not m:
            continue
        h = m.group(1)
        if len(h) == 40:
            return h.lower()
        if len(h) == 32:
            return base64.b32decode(h.upper()).hex()
    return None


def build_magnet(info_hash_hex: str, title: str = "", trackers: list[str] | None = None) -> str:
    m = f"magnet:?xt=urn:btih:{info_hash_hex}"
    if title:
        m += f"&dn={quote(title)}"
    for t in trackers or []:
        m += f"&tr={quote(t, safe='')}"
    return m


def encode(v) -> bytes:
    """Bencode encoder (used by tests to fabricate .torrent files)."""
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(encode(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, x) for k, x in v.items())
        return b"d" + b"".join(encode(k) + encode(x) for k, x in items) + b"e"
    raise TypeError(type(v))
