"""Quality definitions, tiers and profiles."""
from dataclasses import dataclass

# (name, tier, min MB/min, max MB/min). WEB-DL and WEBRip share a tier, like Sonarr.
QUALITIES: list[tuple[str, int, float, float]] = [
    ("Unknown", 0, 0, 0),
    ("SDTV", 1, 1.5, 100),
    ("DVD", 2, 1.5, 100),
    ("WEBRip-480p", 3, 1.5, 100),
    ("WEBDL-480p", 3, 1.5, 100),
    ("Bluray-480p", 4, 1.5, 100),
    ("HDTV-720p", 5, 2.5, 125),
    ("WEBRip-720p", 6, 2.5, 130),
    ("WEBDL-720p", 6, 2.5, 130),
    ("Bluray-720p", 7, 3, 130),
    ("HDTV-1080p", 8, 3, 125),
    ("WEBRip-1080p", 9, 3, 135),
    ("WEBDL-1080p", 9, 3, 135),
    ("Bluray-1080p", 10, 3.5, 160),
    ("Remux-1080p", 11, 25, 400),
    ("HDTV-2160p", 12, 12, 350),
    ("WEBRip-2160p", 13, 12, 400),
    ("WEBDL-2160p", 13, 12, 400),
    ("Bluray-2160p", 14, 15, 400),
    ("Remux-2160p", 15, 35, 500),
]
BY_NAME = {q[0]: q for q in QUALITIES}

# Never acceptable — pre-release recordings.
REJECTED_SOURCES = {"cam", "telesync", "telecine", "screener", "workprint"}


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    allowed: tuple[str, ...]
    cutoff: str  # stop upgrading once a file reaches this tier
    description: str = ""


def _q(*names: str) -> tuple[str, ...]:
    return names


PROFILES: dict[str, Profile] = {p.id: p for p in [
    Profile("any", "Any", _q("SDTV", "DVD", "WEBRip-480p", "WEBDL-480p", "Bluray-480p", "HDTV-720p",
                             "WEBRip-720p", "WEBDL-720p", "Bluray-720p", "HDTV-1080p", "WEBRip-1080p",
                             "WEBDL-1080p", "Bluray-1080p", "HDTV-2160p", "WEBRip-2160p", "WEBDL-2160p",
                             "Bluray-2160p"), "WEBDL-1080p", "Anything watchable, upgrades to 1080p"),
    Profile("sd", "SD", _q("SDTV", "DVD", "WEBRip-480p", "WEBDL-480p", "Bluray-480p"), "WEBDL-480p",
            "Small files, standard definition"),
    Profile("hd-720p", "HD 720p", _q("HDTV-720p", "WEBRip-720p", "WEBDL-720p", "Bluray-720p"), "WEBDL-720p",
            "720p only"),
    Profile("hd-1080p", "HD 1080p", _q("HDTV-1080p", "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p"),
            "WEBDL-1080p", "Prefers WEB-DL, then BluRay"),
    Profile("hd", "HD 720p/1080p", _q("HDTV-720p", "WEBRip-720p", "WEBDL-720p", "Bluray-720p", "HDTV-1080p",
                                      "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p"), "WEBDL-1080p",
            "Takes 720p, upgrades to 1080p"),
    Profile("uhd", "Ultra HD 4K", _q("HDTV-2160p", "WEBRip-2160p", "WEBDL-2160p", "Bluray-2160p"), "WEBDL-2160p",
            "2160p only"),
    Profile("hd-uhd", "HD 1080p + 4K", _q("HDTV-1080p", "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p",
                                          "HDTV-2160p", "WEBRip-2160p", "WEBDL-2160p", "Bluray-2160p"),
            "WEBDL-2160p", "Takes 1080p, upgrades to 4K"),
    Profile("remux", "Remux (huge files)", _q("Bluray-1080p", "Remux-1080p", "Bluray-2160p", "Remux-2160p"),
            "Remux-2160p", "Lossless disc copies"),
]}


def tier(name: str | None) -> int:
    return BY_NAME.get(name or "Unknown", BY_NAME["Unknown"])[1]


def profile(pid: str | None) -> Profile:
    return PROFILES.get(pid or "", PROFILES["hd-1080p"])


def size_limits(name: str) -> tuple[float, float]:
    q = BY_NAME.get(name, BY_NAME["Unknown"])
    return q[2], q[3]


def quality_name(source: str | None, resolution: int | None) -> str:
    """Map parsed source + resolution to a quality name."""
    res = resolution or 0
    if res >= 2000:
        r = "2160p"
    elif res >= 1000:
        r = "1080p"
    elif res >= 700:
        r = "720p"
    elif res:
        r = "480p"
    else:
        r = ""
    if source == "remux":
        return f"Remux-{r or '1080p'}" if (r in ("1080p", "2160p") or not r) else f"Bluray-{r}"
    if source == "bluray":
        return f"Bluray-{r or '480p'}"
    if source == "webdl":
        return f"WEBDL-{r or '480p'}"
    if source == "webrip":
        return f"WEBRip-{r or '480p'}"
    if source == "dvd":
        return "DVD" if r in ("", "480p") else f"Bluray-{r}"
    if source == "hdtv":
        return "SDTV" if r in ("", "480p") else f"HDTV-{r}"
    if r == "480p":
        return "SDTV"
    if r:
        return f"HDTV-{r}"
    return "Unknown"


def public_profiles() -> list[dict]:
    return [{"id": p.id, "name": p.name, "description": p.description, "allowed": list(p.allowed),
             "cutoff": p.cutoff} for p in PROFILES.values()]
