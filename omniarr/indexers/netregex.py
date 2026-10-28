"""Translate .NET-flavoured regexes (as used by Prowlarr definitions) to Python's re."""
from __future__ import annotations

import functools
import re

_UNICODE_CLASSES = {
    "IsCJKUnifiedIdeographs": "\u4e00-\u9fff",
    "IsHiragana": "\u3040-\u309f",
    "IsKatakana": "\u30a0-\u30ff",
    "IsCyrillic": "\u0400-\u04ff",
    "IsArabic": "\u0600-\u06ff",
    "IsHebrew": "\u0590-\u05ff",
    "IsGreek": "\u0370-\u03ff",
    "IsThai": "\u0e00-\u0e7f",
    "IsHangulSyllables": "\uac00-\ud7af",
    "IsBasicLatin": "\u0000-\u007f",
    "L": "A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af",
    "Lu": "A-Z\u00c0-\u00de",
    "Ll": "a-z\u00df-\u00ff",
    "N": "0-9",
    "Nd": "0-9",
    "P": "!-/:-@\\[-`{-~",
}


def _class_body(name: str) -> str:
    return _UNICODE_CLASSES.get(name, "\u4e00-\u9fff")


def translate(pattern: str) -> str:
    p = pattern
    p = re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", p)              # named groups
    p = re.sub(r"\\k<([A-Za-z_]\w*)>", r"(?P=\1)", p)                # named backrefs

    # \p{X} / \P{X} — inside or outside character classes
    def repl_p(m: re.Match) -> str:
        neg, name = m.group(1) == "P", m.group(2)
        body = _class_body(name)
        return f"[^{body}]" if neg else f"[{body}]"

    out, i, in_class = [], 0, False
    while i < len(p):
        c = p[i]
        if c == "\\" and i + 1 < len(p):
            m = re.match(r"\\([pP])\{(\w+)\}", p[i:])
            if m:
                if in_class:
                    out.append(_class_body(m.group(2)))      # negation inside class is approximated
                else:
                    out.append(repl_p(m))
                i += m.end()
                continue
            out.append(p[i:i + 2])
            i += 2
            continue
        if c == "[" and not in_class:
            in_class = True
        elif c == "]" and in_class:
            in_class = False
        out.append(c)
        i += 1
    p = "".join(out)
    p = p.replace("(?i:", "(?i:")  # same syntax
    # Inline flags in the middle of a pattern are an error in Python 3.11+; hoist leading-only ones
    flags = ""
    m = re.match(r"^\(\?([imsx]+)\)", p)
    if m:
        flags, p = m.group(1), p[m.end():]
    mid = re.findall(r"\(\?([imsx]+)\)", p)
    if mid:
        flags += "".join(mid)
        p = re.sub(r"\(\?[imsx]+\)", "", p)
    if flags:
        p = f"(?{''.join(sorted(set(flags)))})" + p
    return p


@functools.lru_cache(maxsize=4096)
def compile_net(pattern: str) -> re.Pattern:
    try:
        return re.compile(translate(pattern))
    except re.error:
        try:
            return re.compile(pattern)
        except re.error:
            return re.compile(re.escape(pattern))


def net_replacement(rep: str) -> str:
    """Convert .NET replacement syntax ($1, ${name}, $$) to Python's."""
    rep = rep.replace("\\", "\\\\")
    rep = re.sub(r"\$\{(\w+)\}", r"\\g<\1>", rep)
    rep = re.sub(r"\$(\d+)", r"\\g<\1>", rep)
    rep = rep.replace("$$", "$")
    return rep
