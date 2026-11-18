"""Refresh state of each macro collector: last attempt, result, and whether a failure was pushed.

Kept in DATA_DIR so a redeploy keeps the backoff and does not repeat a pushed failure.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from buysignal.config import REFRESH_RETRY_BACKOFF
from buysignal.redaction import redact


log = logging.getLogger(__name__)


class Refreshable(Protocol):
    """All the journal needs of a collector: its name, and any domain rule of its own."""

    name: str

    def demands_refresh(self) -> bool: ...


@dataclass(frozen=True)
class CollectorStatus:
    """One collector's last attempt, as /status renders it."""

    name: str
    attempted_at: datetime | None
    ok: bool
    error: str | None


@dataclass
class _Entry:
    attempted_at: datetime
    ok: bool
    error: str | None
    reported_on: date | None  # the date this failure was last pushed, for the once-a-day dedup


class RefreshJournal:
    """Refresh schedule and failure log of every macro collector, on disk."""

    def __init__(self, path: Path | str, backoff: timedelta = REFRESH_RETRY_BACKOFF):
        self._path = Path(path)
        self._backoff = backoff
        self._lock = threading.Lock()  # /refresh runs on the Telegram thread, ticks on the main one
        self._entries = self._load()

    def due(self, collector: Refreshable, now: datetime) -> bool:
        """Whether `collector` should be attempted now."""
        with self._lock:
            entry = self._entries.get(collector.name)
            if entry is not None and not entry.ok and now - entry.attempted_at < self._backoff:
                return False  # still backing off

        if collector.demands_refresh():  # outside the lock: domain rules read files of their own
            return True
        if entry is None:
            return True
        if not entry.ok:
            return True  # retry after the backoff
        return entry.attempted_at.date() != now.date()

    def record(self, name: str, ok: bool, error: str | None, now: datetime) -> None:
        with self._lock:
            previous = self._entries.get(name)
            self._entries[name] = _Entry(
                attempted_at=now,
                ok=ok,
                error=None if ok else redact(error or "unknown error"),  # the journal is a file on disk
                # Carried over so a failure re-recorded hourly is still only pushed once a day.
                reported_on=previous.reported_on if previous is not None else None,
            )
            self._save()

    def unreported_failures(self, now: datetime) -> list[tuple[str, str]]:
        """(name, error) of every failing collector not yet reported today.

        The caller marks only what it delivered, so a failure sent while Telegram is down stays pending.
        """
        with self._lock:
            return [
                (name, entry.error or "unknown error")
                for name, entry in self._entries.items()
                if not entry.ok and entry.reported_on != now.date()
            ]

    def mark_reported(self, name: str, now: datetime) -> None:
        """Record that `name`'s current failure reached someone today, so it isn't sent again."""
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return
            entry.reported_on = now.date()
            self._save()

    def statuses(self) -> list[CollectorStatus]:
        """Every collector's last attempt, in the order they were first recorded."""
        with self._lock:
            return [
                CollectorStatus(name, entry.attempted_at, entry.ok, entry.error)
                for name, entry in self._entries.items()
            ]

    def _load(self) -> dict[str, _Entry]:
        """The journal on disk; missing or corrupt reads as empty, so everything is attempted once."""
        try:
            raw = json.loads(self._path.read_text())
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}

        entries: dict[str, _Entry] = {}
        for name, payload in raw.items():
            try:
                reported_on = payload.get("reported_on")
                entries[name] = _Entry(
                    attempted_at=datetime.fromisoformat(payload["attempted_at"]),
                    ok=bool(payload["ok"]),
                    error=payload.get("error"),
                    reported_on=date.fromisoformat(reported_on) if reported_on else None,
                )
            except Exception:
                continue  # one unreadable entry shouldn't cost the others theirs
        return entries

    def _save(self) -> None:
        """Write the journal; the caller holds the lock."""
        payload = {
            name: {
                "attempted_at": entry.attempted_at.isoformat(),
                "ok": entry.ok,
                "error": entry.error,
                "reported_on": entry.reported_on.isoformat() if entry.reported_on else None,
            }
            for name, entry in self._entries.items()
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            os.replace(tmp, self._path)  # atomic: a torn write would read back as "no journal"
        except OSError as exc:
            # Losing the journal costs backoff and dedup, not the refresh itself; keep going.
            log.error("Refresh journal write failed (%s: %s); state is in-memory only.", type(exc).__name__, exc)
