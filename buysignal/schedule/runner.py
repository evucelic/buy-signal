"""Decide WHEN to act, then compute and report the buy signal.

One tick() per call: hourly, the highest frequency any signal needs (VIX and market_dip
refresh every tick). `python -m buysignal.schedule.runner` does a single tick, for an external
scheduler (cron, GitHub Actions, /loop); the same command with --loop runs tick() forever
itself, sleeping until the next wall-clock hour between calls. What session it is - and
whether VIX is still moving on its own clock, roughly 02:00-20:00 CT, outside NYSE hours -
comes from market_hours.py, so holidays, early closes and DST are handled there.

Full recompute pre-open and post-close; during the regular session just track VIX, recomputing
macro only on a band crossing. When each macro collector is actually attempted is decided in
one place, RefreshJournal (schedule/journal.py): once a day on success, sooner on a domain rule
(margin_debt's FINRA checkpoint, see MARGIN_REFRESH_WINDOW_DAY / data_freshness()), later on a
failure (a retry backoff, so one bad pass costs hours rather than the whole day). So a VIX band
crossing that persists for several hourly ticks only triggers one real refresh, not one per
tick, and a collector that still has work to do gets its chance from an ordinary REGULAR tick.
"""

import logging
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from time import monotonic, sleep, time as wall_time

import requests

from buysignal.config import CF_BYPASS_URL, REFRESH_JOURNAL_JSON, TICK_INTERVAL_SEC
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
from buysignal.signals.buy_signal import compute_signal

log = logging.getLogger(__name__)


def _cf_bypass_ready(timeout: float = 30.0) -> bool:
    """Poll the (compose-managed, always-running) bypass service until it accepts connections."""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            requests.get(CF_BYPASS_URL, timeout=2)
            return True
        except requests.exceptions.ConnectionError:
            sleep(1)
    log.warning("cfbypass never became reachable at %s", CF_BYPASS_URL)
    return False


def _no_precondition() -> str | None:
    return None


def _cf_bypass_precondition() -> str | None:
    if _cf_bypass_ready():
        return None
    return f"cfbypass service unreachable at {CF_BYPASS_URL}"


def _never() -> bool:
    return False


@dataclass(frozen=True)
class MacroCollector:
    """One refreshable macro cache: its refresh, any precondition to refreshing, and any domain
    rule that demands a refresh sooner than the journal's once-a-day schedule would.
    """

    name: str
    update: Callable[[], str | None]
    demands_refresh: Callable[[], bool] = field(default=_never)
    precondition: Callable[[], str | None] = field(default=_no_precondition)


def _macro_collectors() -> list[MacroCollector]:
    """The macro caches refresh_macro() drives, cheapest and most reliable first, so a browser
    that won't start costs only the two collectors that need one.
    """
    from buysignal.collectors import fed_rate, margin_debt, sectors, valuations, yield_curve

    return [
        MacroCollector("yield_curve", yield_curve.update_yield_curve_data, yield_curve.demands_refresh),
        MacroCollector("sector", sectors.update_sector_data),
        MacroCollector(
            "margin_debt",
            margin_debt.update_margin_debt_data,
            margin_debt.demands_refresh,
            _cf_bypass_precondition,
        ),
        MacroCollector("fed_rate", fed_rate.update_fed_rate_data),
        MacroCollector("valuations", valuations.update_valuations_data),
    ]


_journal: RefreshJournal | None = None
_journal_lock = Lock()


def macro_journal() -> RefreshJournal:
    """The process-wide refresh journal, opened on first use (not at import, so tests that
    never refresh never touch the real data directory).

    Locked because the Telegram poll thread (/status, /refresh) and the tick loop can both be
    the first to ask: two instances would each rewrite the file from their own half of it.
    """
    global _journal
    with _journal_lock:
        if _journal is None:
            _journal = RefreshJournal(REFRESH_JOURNAL_JSON)
        return _journal


def refresh_macro(
    force: bool = False,
    journal: RefreshJournal | None = None,
    now: datetime | None = None,
    only: Collection[str] | None = None,
) -> list[tuple[str, str]]:
    """Refresh the slow macro indicators (#2-#5, #7), each one only if the journal says it's due.

    force=True bypasses that gate (the Telegram /refresh command); `only` narrows the pass to
    named collectors (/whattobuy needs valuations, nothing else). Every attempt is recorded,
    success or failure, and one collector blowing up is isolated from the rest of the pass:
    before this, an exception from the first collector silently cost all the others their
    refresh. Returns (name, error_message) for every collector attempted but failed.
    """
    journal = macro_journal() if journal is None else journal
    now = now_et(now)

    failed = []
    for collector in _macro_collectors():
        if only is not None and collector.name not in only:
            continue
        if not (force or journal.due(collector, now)):
            continue
        try:
            error = collector.precondition() or collector.update()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        journal.record(collector.name, ok=error is None, error=error, now=now)
        if error:
            failed.append((collector.name, error))
    return failed


def _any_due(now: datetime | None = None) -> bool:
    """Whether any collector has refresh work outstanding, per the journal's schedule.

    A domain rule that raises (a cache too corrupt for its own defensive read) answers "yes":
    the refresh path records and reports failures properly, and nothing here is worth losing
    a whole tick - signal, alert and report included - to.
    """
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
    """One scheduled cycle. Returns the alert result, or None when nothing needs checking.

    Skipped only when the NYSE session is CLOSED and VIX isn't in its active window either:
    VIX moves outside NYSE hours, so tick() stays live for that on its own clock.
    """
    now = now_et(now)
    session = market_session(now)
    if session == CLOSED and not vix_active_window(now):
        log.info("Market closed, VIX quiet; idle.")
        return None

    result = compute_signal()
    current_vix = next((signal for signal in result.subsignals if signal.name == "vix"), None)
    # A broken VIX sub-signal used to mean current_vix is None and *no* macro refresh at all,
    # pre-market included: the session windows now stand on their own, and any collector the
    # journal says is due (a retry past its backoff, margin_debt's checkpoint) gets its pass
    # from an ordinary REGULAR tick too.
    session_driven = session in (PRE_MARKET, AFTER_HOURS)
    vix_driven = current_vix is not None and current_vix.passes
    if session_driven or vix_driven or _any_due(now):
        for name, error in refresh_macro(now=now):
            log.warning("refresh failed: %s: %s", name, error)

    report(result, session, now)
    return result


def report(result, session: str, now: datetime | None = None) -> None:
    now = now_et(now)
    log.info("[%s] session=%s", stamp(now), session)
    log.info(
        "  Alert: %s  (passing %d/%d; score %+.2f)",
        result.state.upper(), result.passing_count, result.required_count, result.score,
    )
    log.info("  Rule: %s", result.detail)
    if result.missing_signals:
        log.info("  Missing: %s", ", ".join(result.missing_signals))
    for s in result.subsignals:
        status = "PASS" if s.passes else "FAIL"
        log.info("    - %-12s %-4s %-11s %s", s.name, status, s.state, s.detail)


def _seconds_until_next_boundary(interval_sec: float) -> float:
    """Seconds until the next wall-clock multiple of interval_sec (e.g. next top-of-hour for
    3600), so ticks land on full hours instead of drifting from whenever the process started.
    """
    if interval_sec <= 0:
        return interval_sec
    return interval_sec - (wall_time() % interval_sec)


def run_forever(interval_sec: float = TICK_INTERVAL_SEC, on_tick=None) -> None:
    """Tick on wall-clock interval_sec boundaries, forever. A failing tick is logged, not fatal.

    If given, on_tick(result, error) is called after every attempt (result is tick()'s return,
    None when the market's closed or the tick failed; error is the caught exception, or None).
    A failing callback is itself logged and skipped, same as a failing tick.
    """
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
