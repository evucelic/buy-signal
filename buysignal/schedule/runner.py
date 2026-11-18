"""Scheduling: when to refresh collectors and compute the checklist, one tick per hour."""

import logging
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from time import sleep, time as wall_time


from buysignal.config import REFRESH_JOURNAL_JSON, TICK_INTERVAL_SEC
from buysignal.schedule.journal import RefreshJournal
from buysignal.schedule.market_hours import (
    AFTER_HOURS,
    CLOSED,
    PRE_MARKET,
    market_session,
    now_et,
    stamp,
    vix_active_window,
)
from buysignal.signals.buy_signal import REQUIRED_SIGNALS, compute_signal

log = logging.getLogger(__name__)


def _never() -> bool:
    return False


@dataclass(frozen=True)
class MacroCollector:
    """A refreshable macro cache, with an optional rule that asks for a refresh before its daily turn."""

    name: str
    update: Callable[[], str | None]
    demands_refresh: Callable[[], bool] = field(default=_never)


def _macro_collectors() -> list[MacroCollector]:
    """Collectors in run order, cheapest first, so a browser that won't start costs only the two that need it."""
    from buysignal.broker import sync
    from buysignal.collectors import credit, fed_rate, margin_debt, sectors, valuations, vix_history, yield_curve

    return [
        MacroCollector("credit", credit.update_credit_data, credit.demands_refresh),
        MacroCollector("yield_curve", yield_curve.update_yield_curve_data, yield_curve.demands_refresh),
        MacroCollector("vix_history", vix_history.update_vix_history),
        MacroCollector("sector", sectors.update_sector_data, sectors.demands_refresh),
        MacroCollector(
            "margin_debt",
            margin_debt.update_margin_debt_data,
            margin_debt.demands_refresh,
        ),
        MacroCollector("fed_rate", fed_rate.update_fed_rate_data, fed_rate.demands_refresh),
        MacroCollector("valuations", valuations.update_valuations_data),
        *([MacroCollector(sync.NAME, sync.update, sync.demands_refresh),
           MacroCollector(sync.HISTORY, sync.backfill, sync.history_due)] if sync.configured() else []),
    ]


_journal: RefreshJournal | None = None
_journal_lock = Lock()


def macro_journal() -> RefreshJournal:
    """The process-wide refresh journal, opened on first use; locked because two threads may ask first."""
    global _journal
    with _journal_lock:
        if _journal is None:
            _journal = RefreshJournal(REFRESH_JOURNAL_JSON)
        return _journal


_macro_refresh_lock = Lock()


def refresh_macro(
    force: bool = False,
    journal: RefreshJournal | None = None,
    now: datetime | None = None,
    only: Collection[str] | None = None,
) -> list[tuple[str, str]]:
    with _macro_refresh_lock:
        return _refresh_macro(force=force, journal=journal, now=now, only=only)


def _refresh_macro(
    force: bool = False,
    journal: RefreshJournal | None = None,
    now: datetime | None = None,
    only: Collection[str] | None = None,
) -> list[tuple[str, str]]:
    """Refresh each due collector (every one with force=True, or those in `only`); return (name, error) per failure."""
    journal = macro_journal() if journal is None else journal
    now = now_et(now)

    failed = []
    for collector in _macro_collectors():
        if only is not None and collector.name not in only:
            continue
        if not (force or journal.due(collector, now)):
            continue
        try:
            error = collector.update()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        journal.record(collector.name, ok=error is None, error=error, now=now)
        if error:
            failed.append((collector.name, error))
    return failed


def _any_due(now: datetime | None = None) -> bool:
    """Whether any collector is due; a rule that raises counts as due, so the refresh path reports it."""
    now = now_et(now)
    for collector in _macro_collectors():
        try:
            if macro_journal().due(collector, now):
                return True
        except Exception as exc:
            log.warning("%s refresh gate failed (%s: %s); attempting it", collector.name, type(exc).__name__, exc)
            return True
    return False


def tick(now: datetime | None = None):
    """One scheduled cycle: the checklist result, or None when neither the NYSE nor VIX is active."""
    now = now_et(now)
    session = market_session(now)
    if session == CLOSED and not vix_active_window(now):
        log.info("Market closed, VIX quiet; idle.")
        return None

    result = compute_signal()
    current_vix = next((signal for signal in result.subsignals if signal.name == "vix"), None)
    session_driven = session in (PRE_MARKET, AFTER_HOURS)
    vix_driven = current_vix is not None and current_vix.passes
    if session_driven or vix_driven or _any_due(now):
        for name, error in refresh_macro(now=now):
            log.warning("refresh failed: %s: %s", name, error)
        # Report the refreshed macro snapshot in this tick, not one hour later.
        # The first computation already fetched VIX/market data; reuse those caches.
        result = compute_signal(allow_refresh=False)

    report(result, session, now)
    return result


def report(result, session: str, now: datetime | None = None) -> None:
    now = now_et(now)
    log.info("[%s] session=%s", stamp(now), session)
    log.info("  Checklist: %s  (%s)", result.label, result.detail)
    if result.missing_signals:
        log.info("  Missing: %s", ", ".join(result.missing_signals))
    for s in result.subsignals:
        status = result.status(s.name) if s.name in REQUIRED_SIGNALS else "context"
        log.info("    - %-12s %-10s %-14s %s", s.name, status, s.state, s.detail)


def _seconds_until_next_boundary(interval_sec: float) -> float:
    """Seconds to the next wall-clock multiple of interval_sec, so ticks land on the hour."""
    if interval_sec <= 0:
        return interval_sec
    return interval_sec - (wall_time() % interval_sec)


def run_forever(interval_sec: float = TICK_INTERVAL_SEC, on_tick=None) -> None:
    """Tick on wall-clock boundaries; on_tick(result, error) runs after each attempt, and failures are logged."""
    log.info("Runner starting: tick every %.0fm (Ctrl+C to stop).", interval_sec / 60)
    try:
        while True:
            result, error = None, None
            try:
                result = tick()
            except Exception as exc:
                error = exc
                log.error("tick failed (%s: %s); retrying next cycle.", type(exc).__name__, exc)
            if on_tick is not None:
                try:
                    on_tick(result, error)
                except Exception as exc:
                    log.error("on_tick callback failed (%s: %s)", type(exc).__name__, exc)
            sleep(_seconds_until_next_boundary(interval_sec))
    except KeyboardInterrupt:
        log.info("Runner stopped.")


if __name__ == "__main__":
    from buysignal import logs

    logs.configure()  # __main__.py does this for the long-running process; do it for the CLI too
    if "--loop" in sys.argv:
        run_forever()
    else:
        tick()
