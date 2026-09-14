"""Logging setup for the whole process: one configure() call, from __main__.py, before anything else.

Collector output used to be bare print()s with no timestamp at all, so `docker compose logs`
couldn't be lined up against the tick schedule. Everything goes through logging now: to stderr,
so `docker compose logs` keeps working, and to a rotating file on the data volume, which
survives the container recreation every deploy does.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from buysignal.config import DATA_DIR

LOG_FILE = DATA_DIR / "buysignal.log"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_OURS = "_buysignal_handler"  # marks the handlers configure() owns, so a re-call replaces them

# Libraries that log a line per socket at DEBUG; LOG_LEVEL=DEBUG is for our own retry chatter,
# not for urllib3's connection pool.
_NOISY = ("urllib3", "charset_normalizer", "matplotlib", "selenium", "yfinance", "peewee")


def configure(path: Path | str = LOG_FILE, level: str | None = None) -> None:
    """Log to stderr and to a rotating file at `path`, at LOG_LEVEL (default INFO).

    Idempotent: calling it again replaces the handlers it installed last time rather than
    stacking a second copy of every line on top of them.
    """
    wanted = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    resolved = logging.getLevelName(wanted)  # an int for a known name, "Level FOO" otherwise
    root.setLevel(resolved if isinstance(resolved, int) else logging.INFO)

    for handler in [h for h in root.handlers if getattr(h, _OURS, False)]:
        root.removeHandler(handler)
        handler.close()

    if not isinstance(resolved, int):
        root.warning("Unknown LOG_LEVEL %r; logging at INFO", wanted)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _install(root, logging.StreamHandler())
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _install(root, RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT))
    except OSError as exc:
        # No log file is survivable; no logging at all isn't. Keep stderr and say why.
        root.warning("File logging disabled, %s not writable (%s: %s)", path, type(exc).__name__, exc)


def _install(root: logging.Logger, handler: logging.Handler) -> None:
    handler.setFormatter(logging.Formatter(FORMAT))
    setattr(handler, _OURS, True)
    root.addHandler(handler)
