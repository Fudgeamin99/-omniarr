"""Logging to a rotating file plus an in-memory buffer the UI can show."""
import collections
import logging
import logging.handlers
import time

from . import config

_buffer: collections.deque = collections.deque(maxlen=1500)


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            _buffer.append({
                "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name.replace("omniarr.", ""),
                "message": msg,
            })
        except Exception:  # pragma: no cover - logging must never raise
            pass


def setup(level: int = logging.INFO) -> None:
    config.ensure_dirs()
    root = logging.getLogger("omniarr")
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "omniarr.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(level)
    bh = _BufferHandler()
    bh.setLevel(logging.INFO)
    for h in (fh, sh, bh):
        root.addHandler(h)


def recent(limit: int = 300, level: str | None = None) -> list[dict]:
    items = list(_buffer)
    if level:
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        minimum = order.index(level.upper()) if level.upper() in order else 0
        items = [i for i in items if order.index(i["level"]) >= minimum]
    return items[-limit:][::-1]


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"omniarr.{name}")
