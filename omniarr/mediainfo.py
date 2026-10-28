"""Find embedded subtitle tracks (MKV parsed directly; other containers via ffprobe if installed)."""
from __future__ import annotations

import json
import shutil
import struct
import subprocess

SEGMENT, TRACKS, TRACK_ENTRY, CLUSTER = 0x18538067, 0x1654AE6B, 0xAE, 0x1F43B675
TRACK_TYPE, LANGUAGE, LANGUAGE_IETF, FLAG_FORCED, NAME, CODEC = 0x83, 0x22B59C, 0x22B59D, 0x55AA, 0x536E, 0x86


def _vint(buf: bytes, pos: int, keep_marker: bool) -> tuple[int, int]:
    first = buf[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("bad vint")
    value = first if keep_marker else first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    return value, length


def _elements(buf: bytes, start: int, end: int):
    pos = start
    while pos < end - 1:
        eid, l1 = _vint(buf, pos, True)
        size, l2 = _vint(buf, pos + l1, False)
        data_start = pos + l1 + l2
        unknown = size == (1 << (7 * l2)) - 1
        data_end = end if unknown else data_start + size
        yield eid, data_start, min(data_end, end), unknown
        if unknown:
            return
        pos = data_end


def _uint(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def mkv_subtitle_tracks(path: str, max_bytes: int = 8 * 1024 * 1024) -> list[dict]:
    with open(path, "rb") as fh:
        buf = fh.read(max_bytes)
    if buf[:4] != b"\x1a\x45\xdf\xa3":
        raise ValueError("not an EBML file")
    tracks: list[dict] = []
    for eid, s, e, _u in _elements(buf, 0, len(buf)):
        if eid != SEGMENT:
            continue
        for cid, cs, ce, _cu in _elements(buf, s, e):
            if cid == CLUSTER:
                return tracks
            if cid != TRACKS:
                continue
            for tid, ts, te, _tu in _elements(buf, cs, ce):
                if tid != TRACK_ENTRY:
                    continue
                info = {"type": None, "language": "eng", "forced": False, "name": "", "codec": ""}
                for fid, fs, fe, _fu in _elements(buf, ts, te):
                    raw = buf[fs:fe]
                    if fid == TRACK_TYPE:
                        info["type"] = _uint(raw)
                    elif fid == LANGUAGE:
                        info["language"] = raw.rstrip(b"\x00").decode("ascii", "ignore") or "eng"
                    elif fid == LANGUAGE_IETF:
                        info["language_ietf"] = raw.rstrip(b"\x00").decode("ascii", "ignore")
                    elif fid == FLAG_FORCED:
                        info["forced"] = bool(_uint(raw))
                    elif fid == NAME:
                        info["name"] = raw.decode("utf-8", "ignore")
                    elif fid == CODEC:
                        info["codec"] = raw.decode("ascii", "ignore")
                if info["type"] == 17:
                    tracks.append(info)
            return tracks
    return tracks


def _ffprobe(path: str) -> list[dict]:
    exe = shutil.which("ffprobe")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-v", "error", "-select_streams", "s", "-show_entries",
                              "stream=codec_name:stream_tags=language,title:stream_disposition=forced",
                              "-of", "json", path], capture_output=True, timeout=60, check=True).stdout
        data = json.loads(out or b"{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return []
    res = []
    for s in data.get("streams", []):
        tags = s.get("tags") or {}
        res.append({"language": tags.get("language") or "und", "name": tags.get("title") or "",
                    "forced": bool((s.get("disposition") or {}).get("forced")), "codec": s.get("codec_name")})
    return res


_LANG3 = {"en": {"eng", "en", "en-us", "en-gb"}}


def embedded_languages(path: str) -> set[str]:
    """Two-letter codes of embedded, non-forced subtitle tracks."""
    tracks: list[dict] = []
    if path.lower().endswith((".mkv", ".mka", ".webm")):
        try:
            tracks = mkv_subtitle_tracks(path)
        except (OSError, ValueError, IndexError, struct.error):
            tracks = _ffprobe(path)
    else:
        tracks = _ffprobe(path)
    langs = set()
    for t in tracks:
        if t.get("forced"):
            continue
        code = (t.get("language_ietf") or t.get("language") or "").lower()
        for two, names in _LANG3.items():
            if code in names or code.startswith(two + "-"):
                langs.add(two)
    return langs


def opensubtitles_hash(path: str) -> str | None:
    """The 64-bit OpenSubtitles hash: size + sum of first and last 64 KiB as little-endian uint64s."""
    block = 65536
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size < block * 2:
                return None
            h = size
            for offset in (0, size - block):
                f.seek(offset)
                data = f.read(block)
                for (v,) in struct.iter_unpack("<Q", data):
                    h = (h + v) & 0xFFFFFFFFFFFFFFFF
        return f"{h:016x}"
    except OSError:
        return None
