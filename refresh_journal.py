"""How each macro cache is doing: when it was last attempted, whether it worked, whether that
failure has already been pushed.

Attempt state, retry timing and notification dedup are one fact — "how is this collector
doing" — so one module owns all three instead of smearing them across a file's mtime, a dict
in runner and a second dict in telegram_bot. It persists to DATA_DIR, the volume the caches
themselves live on, so a redeploy neither resets the retry backoff nor re-pushes a failure you
were already told about.

`now` is always a parameter: nothing here reads the wall clock, so the tests are literal
timestamps against a tmp file rather than a monkeypatched clock.
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

from config import REFRESH_RETRY_BACKOFF


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
    """The refresh schedule and the failure log of every macro collector, on disk."""

    def __init__(self, path: Path | str, backoff: timedelta = REFRESH_RETRY_BACKOFF):
        self._path = Path(path)
        self._backoff = backoff
        self._lock = threading.Lock()  # /refresh runs on the Telegram thread, ticks on the main one
        self._entries = self._load()

    def due(self, collector: Refreshable, now: datetime) -> bool:
        """Whether `collector` should be attempted now: the one place refresh timing is decided."""
        with self._lock:
            entry = self._entries.get(collector.name)
            if entry is not None and not entry.ok and now - entry.attempted_at < self._backoff:
                return False  # broken right now; no domain rule argues with that

        if collector.demands_refresh():  # outside the lock: domain rules read files of their own
            return True
        if entry is None:
            return True
        if not entry.ok:
            return True  # failed, and past the backoff checked above: this is the retry
        return entry.attempted_at.date() != now.date()

    def record(self, name: str, ok: bool, error: str | None, now: datetime) -> None:
        """Record one attempt and its outcome, whatever it was."""
        with self._lock:
            previous = self._entries.get(name)
            self._entries[name] = _Entry(
                attempted_at=now,
                ok=ok,
                error=None if ok else (error or "unknown error"),
                # Carried over so a failure re-recorded hourly is still only pushed once a day.
                reported_on=previous.reported_on if previous is not None else None,
            )
            self._save()

    def take_unreported_failures(self, now: datetime) -> list[tuple[str, str]]:
        """The (name, error) of every currently-failing collector not yet reported today.

        Deliberately a mutating query: marking them reported is the job, so callers need no
        dedup state of their own.
        """
        taken = []
        with self._lock:
            for name, entry in self._entries.items():
                if entry.ok or entry.reported_on == now.date():
                    continue
                taken.append((name, entry.error or "unknown error"))
                entry.reported_on = now.date()
            if taken:
                self._save()
        return taken

    def statuses(self) -> list[CollectorStatus]:
        """Every collector's last attempt, in the order they were first recorded."""
        with self._lock:
            return [
                CollectorStatus(name, entry.attempted_at, entry.ok, entry.error)
                for name, entry in self._entries.items()
            ]

    # --- persistence -----------------------------------------------------------------------

    def _load(self) -> dict[str, _Entry]:
        """The journal on disk. Missing, corrupt or half-written all degrade to empty, i.e.
        "attempt everything once" — the safe default, and the same defensive read
        _cached_policy_spread() uses in collectors/yield_curve.py.
        """
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
        """Write the journal out. Caller holds the lock."""
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
